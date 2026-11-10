"""Single global worker; no persisted queue, passwords, history or downloads."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from collections.abc import Awaitable, Callable


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

    async def run(self) -> None:
        while True:
            job = await self.queue.get()
            self.active = True
            try:
                await self.processor(job)
            except Exception:
                # Never print user URL, Telegram credentials, raw extractor errors.
                try:
                    await self.on_error(job)
                except Exception:
                    pass
            finally:
                self.active = False
                self.queue.task_done()
