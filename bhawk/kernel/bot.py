"""The bot kernel: wires together sharding, both databases, and all services.

Key v1 -> v2 change: `HawkBot` now extends `commands.AutoShardedBot` instead
of `commands.Bot`. discord.py transparently manages one gateway connection
per shard inside a single process; combined with `Settings.shard_id_list`,
the *same* process image can also be launched multiple times (one per
cluster/host) each owning a disjoint shard range — see main.py and
MIGRATION.md ("Scaling to multiple processes").
"""
from __future__ import annotations

import datetime
from pkgutil import iter_modules

import discord
from discord.ext import commands

import bhawk.cogs as cogs_package
from bhawk.config import Settings
from bhawk.db.mongo import MongoDatabaseManager
from bhawk.db.postgres import PostgresDatabaseManager
from bhawk.kernel.context import HawkContext
from bhawk.logging_setup import get_logger
from bhawk.managers.language import LanguageManager
from bhawk.security import SlidingWindowRateLimiter
from bhawk.error_reporting import ErrorReporter
from bhawk.blacklist import BlacklistedError, BlacklistManager
from bhawk.toolkit import ToolKit

logger = get_logger(__name__)


class HawkBot(commands.AutoShardedBot):
    """Sharded Discord client wiring together persistence, i18n, and utilities."""

    def __init__(self, *args: object, settings: Settings, **kwargs: object) -> None:
        super().__init__(
            *args,
            allowed_installs=discord.app_commands.AppInstallationType(guild=True, user=False),
            allowed_contexts=discord.app_commands.AppCommandContext(guild=True, dm_channel=False, private_channel=False),
            **kwargs,
        )
        self.settings = settings # store settings for later use
        self.start_time = datetime.datetime.now(datetime.UTC)
        self.slash_cache: list[discord.app_commands.AppCommand] = []

        self.blacklist = BlacklistManager(self)
        self.toolkit = ToolKit(self)
        self.errors = ErrorReporter(self, settings.error_webhook_url) # Webhook for error reporting (in discord.)
        self.db = MongoDatabaseManager(uri=settings.mongo_uri, db_name=settings.mongo_db_name)
        self.sql = PostgresDatabaseManager(
            dsn=settings.postgres_dsn, min_size=settings.postgres_pool_min, max_size=settings.postgres_pool_max
        )
        self.language = LanguageManager(locales_path="locales", default_language="es")
        self.rate_limiter = SlidingWindowRateLimiter(max_calls=settings.command_rate_limit_per_minute)

    async def get_context(
        self, origin: discord.Message | discord.Interaction, *, cls: type[commands.Context] = HawkContext
    ) -> HawkContext:
        return await super().get_context(origin, cls=cls)  # type: ignore[return-value]

    async def _global_blacklist_check(self, ctx: HawkContext) -> bool:
        """Registered as the very first bot-wide check (see setup_hook) so
        it always runs before any other check/handler, guaranteeing total
        silence for blacklisted entities regardless of what else applies."""
        if await self.is_owner(ctx.author):
            return True
        guild_id = ctx.guild.id if ctx.guild else None
        if self.blacklist.is_blacklisted(ctx.author.id, guild_id):
            raise BlacklistedError()
        return True

    async def setup_hook(self) -> None:
        """discord.py lifecycle hook: run once before the first gateway connection."""
        await self.toolkit.setup()
        await self.sql.connect()
        await self.db.connect()
        await self.blacklist.load()
        self.add_check(self._global_blacklist_check)
        self.errors.start()
        await self.load_extension("jishaku")

        for module_info in iter_modules(cogs_package.__path__):
            if module_info.ispkg:
                continue
            await self.load_extension(f"bhawk.cogs.{module_info.name}")
        logger.info("cogs_loaded")

        self.slash_cache = await self.tree.sync()
        logger.info(
            "bot_ready_to_connect",
            shard_count=self.shard_count,
            cluster_id=self.settings.cluster_id,
        )

    async def close(self) -> None:
        """discord.py lifecycle hook: run once on shutdown. Closes every resource
        symmetrically with what setup_hook() opened."""
        await self.errors.stop()
        await self.toolkit.close()
        await self.sql.close()
        await self.db.close()
        await super().close()
