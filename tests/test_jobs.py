import asyncio
import unittest

from jobs import GlobalFIFO, Job


class QueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_jobs_really_sequential_across_users(self):
        started = asyncio.Event()
        release = asyncio.Event()
        actions = []

        async def processor(job):
            actions.append(("start", job.chat_id))
            if job.chat_id == 1:
                started.set()
                await release.wait()
            actions.append(("finish", job.chat_id))

        async def error(job):
            raise AssertionError("unexpected")

        q = GlobalFIFO(limit=10, processor=processor, on_error=error)
        worker = asyncio.create_task(q.run())
        try:
            self.assertEqual(q.add(Job(1, "https://youtu.be/abc")), 1)
            self.assertEqual(q.add(Job(2, "https://youtu.be/def")), 2)
            await asyncio.wait_for(started.wait(), 1)
            await asyncio.sleep(0.05)
            self.assertEqual(actions, [("start", 1)])
            release.set()
            await asyncio.wait_for(q.queue.join(), 1)
            self.assertEqual(actions, [("start", 1), ("finish", 1), ("start", 2), ("finish", 2)])
        finally:
            worker.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await worker

    async def test_failure_does_not_kill_worker(self):
        errors = []
        successes = []

        async def processor(job):
            if job.chat_id == 10:
                raise RuntimeError("simulated")
            successes.append(job.chat_id)

        async def on_error(job):
            errors.append(job.chat_id)

        q = GlobalFIFO(limit=2, processor=processor, on_error=on_error)
        worker = asyncio.create_task(q.run())
        try:
            q.add(Job(10, "bad"))
            q.add(Job(20, "good"))
            self.assertIsNone(q.add(Job(30, "full")))
            await asyncio.wait_for(q.queue.join(), 1)
            self.assertEqual(errors, [10])
            self.assertEqual(successes, [20])
        finally:
            worker.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await worker


if __name__ == "__main__":
    unittest.main()
