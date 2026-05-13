"""Periodic ingestion using APScheduler asyncio integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

if TYPE_CHECKING:
    from app.ingestion.pipeline import IngestionPipeline

logger = logging.getLogger(__name__)


class IngestionScheduler:
    """Runs ``IngestionPipeline.ingest_all`` on an interval."""

    def __init__(
        self,
        pipeline: "IngestionPipeline",
        interval_minutes: int,
    ) -> None:
        self._pipeline = pipeline
        self._interval = max(1, int(interval_minutes))
        self._scheduler = AsyncIOScheduler()
        logger.debug("ingestion_scheduler_created interval_minutes=%s", self._interval)

    @property
    def interval_minutes(self) -> int:
        return self._interval

    def start(self) -> None:
        self._scheduler.add_job(
            self._run,
            trigger="interval",
            minutes=self._interval,
            id="ingestion_cycle",
            name="Periodic knowledge ingestion",
            replace_existing=True,
        )
        self._scheduler.start()
        logger.info("ingestion_scheduler_started interval_minutes=%s", self._interval)

    async def _run(self) -> None:
        logger.info("scheduled_ingestion_cycle_starting")
        try:
            results = await self._pipeline.ingest_all()
            total_chunks = sum(r.chunks_upserted for r in results)
            logger.info(
                "scheduled_ingestion_complete sources=%s chunks_upserted=%s",
                len(results),
                total_chunks,
            )
        except Exception:
            logger.error("scheduled_ingestion_cycle_failed", exc_info=True)

    def reschedule(self, interval_minutes: int) -> None:
        self._interval = max(1, int(interval_minutes))
        job = self._scheduler.get_job("ingestion_cycle")
        if job:
            self._scheduler.modify_job(
                "ingestion_cycle",
                trigger=IntervalTrigger(minutes=self._interval),
            )
        logger.info("ingestion_scheduler_rescheduled interval_minutes=%s", self._interval)

    def stop(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("ingestion_scheduler_stopped")

    def get_next_run_iso(self) -> str | None:
        job = self._scheduler.get_job("ingestion_cycle")
        if not job or not job.next_run_time:
            return None
        return job.next_run_time.isoformat()

    @property
    def running(self) -> bool:
        return bool(self._scheduler.running)
