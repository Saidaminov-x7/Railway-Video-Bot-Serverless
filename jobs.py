"""Single global worker; no persisted queue, passwords, history or downloads."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

LOG = logging.getLogger("video-bot.queue")


@dataclass(frozen=True)
class Job:
    chat_id: int
    url: str


class GlobalFIFO:
    def __init__(self, limit: int, processor: Callable[[Job], Awaitable[None]], on_error: Callable[[Job], Awaitable[None]]):
        self.queue: asyncio.Queue[Job] = asyncio.Queue(maxsize=limit)
        self.processor = processor
        self.on_error = on_error
        self.active = False

    def add(self, job: Job) -> int | None:
        position = self.queue.qsize() + int(self.active) + 1
        try:
            self.queue.put_nowait(job)
        except asyncio.QueueFull:
            return None
        return position

    def remove_chat(self, chat_id: int) -> int:
        """Remove queued jobs owned by a chat while preserving FIFO for everyone else."""
        retained: list[Job] = []
        removed = 0
        while True:
            try:
                job = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self.queue.task_done()
            if job.chat_id == chat_id:
                removed += 1
            else:
                retained.append(job)
        for job in retained:
            self.queue.put_nowait(job)
        return removed

    async def run(self) -> None:
        while True:
            job = await self.queue.get()
            self.active = True
            try:
                await self.processor(job)
            except Exception as exc:
                # Never print user URL, Telegram credentials, raw extractor errors.
                LOG.warning("job failed: %s", type(exc).__name__)
                try:
                    await self.on_error(job)
                except Exception as notify_error:
                    LOG.warning("job error notification failed: %s", type(notify_error).__name__)
            finally:
                self.active = False
                self.queue.task_done()
