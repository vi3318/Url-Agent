"""
Performance Monitor
====================
Real-time metrics tracking for the async crawler.

Tracks:
- Pages/sec (rolling 30s window + overall)
- Queue size over time
- Retry count and failure rate
- Worker utilization
- Bytes downloaded
- Content extraction quality (words/page)
- Per-phase timing (navigate, extract, enqueue)

Thread-safe: all methods use asyncio.Lock for safe concurrent access.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# Rolling window for pages/sec calculation
_ROLLING_WINDOW_SEC = 30.0


@dataclass
class PageTiming:
    """Timing breakdown for a single page crawl."""
    url: str = ""
    navigate_ms: float = 0.0
    extract_ms: float = 0.0
    enqueue_ms: float = 0.0
    total_ms: float = 0.0
    word_count: int = 0
    link_count: int = 0
    status: str = "ok"   # ok | skipped | failed | timeout


@dataclass
class CrawlMetrics:
    """Snapshot of all crawler metrics at a point in time."""
    # Counts
    pages_crawled: int = 0
    pages_skipped: int = 0
    pages_failed: int = 0
    pages_retried: int = 0
    total_enqueued: int = 0

    # Speed
    pages_per_sec_rolling: float = 0.0
    pages_per_sec_overall: float = 0.0

    # Queue
    queue_size: int = 0
    queue_peak: int = 0

    # Workers
    active_workers: int = 0
    max_workers: int = 0

    # Content quality
    total_words: int = 0
    avg_words_per_page: float = 0.0
    total_links_discovered: int = 0

    # Timing
    avg_page_ms: float = 0.0
    avg_navigate_ms: float = 0.0
    avg_extract_ms: float = 0.0
    p95_page_ms: float = 0.0

    # Resource
    total_bytes: int = 0
    elapsed_sec: float = 0.0

    # Stop reason
    stop_reason: str = ""


class PerformanceMonitor:
    """
    Async-safe performance monitor for the crawler.

    Usage::

        monitor = PerformanceMonitor(max_workers=6)
        await monitor.start()

        # In each worker:
        timing = PageTiming(url=url)
        timing.navigate_ms = ...
        await monitor.record_page(timing)

        # Periodically (or from a reporter coroutine):
        metrics = await monitor.snapshot()

        await monitor.stop()
    """

    def __init__(self, max_workers: int = 6):
        self._lock = asyncio.Lock()
        self._start_time: float = 0.0
        self._max_workers = max_workers

        # Counters
        self._pages_crawled = 0
        self._pages_skipped = 0
        self._pages_failed = 0
        self._pages_retried = 0
        self._total_enqueued = 0
        self._total_words = 0
        self._total_links = 0
        self._total_bytes = 0

        # Queue tracking
        self._queue_size = 0
        self._queue_peak = 0

        # Worker tracking
        self._active_workers = 0

        # Rolling window
        self._recent_timestamps: deque[float] = deque()

        # Page timings (keep last 1000 for percentile calc)
        self._page_timings: deque[PageTiming] = deque(maxlen=1000)

        # Callbacks
        self._progress_callback: Optional[Callable] = None

        # Reporter task
        self._reporter_task: Optional[asyncio.Task] = None
        self._running = False
        self._stop_reason = ""

    def set_progress_callback(self, callback: Callable) -> None:
        """Set callback: callback(metrics: CrawlMetrics)"""
        self._progress_callback = callback

    async def start(self) -> None:
        """Start the monitor and periodic reporter."""
        self._start_time = time.monotonic()
        self._running = True
        self._reporter_task = asyncio.create_task(self._reporter_loop())

    async def stop(self, reason: str = "completed") -> None:
        """Stop the monitor."""
        self._running = False
        self._stop_reason = reason
        if self._reporter_task:
            self._reporter_task.cancel()
            try:
                await self._reporter_task
            except asyncio.CancelledError:
                pass

    async def record_page(self, timing: PageTiming) -> None:
        """Record metrics for a completed page."""
        now = time.monotonic()
        async with self._lock:
            if timing.status == "ok":
                self._pages_crawled += 1
                self._total_words += timing.word_count
            elif timing.status == "skipped":
                self._pages_skipped += 1
            elif timing.status == "failed" or timing.status == "timeout":
                self._pages_failed += 1

            self._total_links += timing.link_count
            self._recent_timestamps.append(now)
            self._page_timings.append(timing)

            # Prune old timestamps from rolling window
            cutoff = now - _ROLLING_WINDOW_SEC
            while self._recent_timestamps and self._recent_timestamps[0] < cutoff:
                self._recent_timestamps.popleft()

    async def record_retry(self) -> None:
        async with self._lock:
            self._pages_retried += 1

    async def record_enqueue(self, count: int = 1) -> None:
        async with self._lock:
            self._total_enqueued += count

    async def record_bytes(self, nbytes: int) -> None:
        async with self._lock:
            self._total_bytes += nbytes

    async def update_queue_size(self, size: int) -> None:
        async with self._lock:
            self._queue_size = size
            if size > self._queue_peak:
                self._queue_peak = size

    async def worker_started(self) -> None:
        async with self._lock:
            self._active_workers += 1

    async def worker_finished(self) -> None:
        async with self._lock:
            self._active_workers = max(0, self._active_workers - 1)

    async def snapshot(self) -> CrawlMetrics:
        """Take a consistent snapshot of all metrics."""
        now = time.monotonic()
        async with self._lock:
            elapsed = now - self._start_time if self._start_time else 0.0

            # Rolling pages/sec
            cutoff = now - _ROLLING_WINDOW_SEC
            while self._recent_timestamps and self._recent_timestamps[0] < cutoff:
                self._recent_timestamps.popleft()
            rolling_count = len(self._recent_timestamps)
            rolling_pps = rolling_count / _ROLLING_WINDOW_SEC if rolling_count else 0.0

            # Overall pages/sec
            total_good = self._pages_crawled
            overall_pps = total_good / elapsed if elapsed > 0 else 0.0

            # Timing stats
            timings = [t.total_ms for t in self._page_timings if t.total_ms > 0]
            avg_page = sum(timings) / len(timings) if timings else 0.0
            nav_times = [t.navigate_ms for t in self._page_timings if t.navigate_ms > 0]
            avg_nav = sum(nav_times) / len(nav_times) if nav_times else 0.0
            ext_times = [t.extract_ms for t in self._page_timings if t.extract_ms > 0]
            avg_ext = sum(ext_times) / len(ext_times) if ext_times else 0.0

            # P95
            p95 = 0.0
            if timings:
                sorted_t = sorted(timings)
                idx = int(len(sorted_t) * 0.95)
                p95 = sorted_t[min(idx, len(sorted_t) - 1)]

            avg_words = self._total_words / total_good if total_good > 0 else 0.0

            return CrawlMetrics(
                pages_crawled=self._pages_crawled,
                pages_skipped=self._pages_skipped,
                pages_failed=self._pages_failed,
                pages_retried=self._pages_retried,
                total_enqueued=self._total_enqueued,
                pages_per_sec_rolling=round(rolling_pps, 2),
                pages_per_sec_overall=round(overall_pps, 2),
                queue_size=self._queue_size,
                queue_peak=self._queue_peak,
                active_workers=self._active_workers,
                max_workers=self._max_workers,
                total_words=self._total_words,
                avg_words_per_page=round(avg_words, 1),
                total_links_discovered=self._total_links,
                avg_page_ms=round(avg_page, 1),
                avg_navigate_ms=round(avg_nav, 1),
                avg_extract_ms=round(avg_ext, 1),
                p95_page_ms=round(p95, 1),
                total_bytes=self._total_bytes,
                elapsed_sec=round(elapsed, 2),
                stop_reason=self._stop_reason,
            )

    async def _reporter_loop(self) -> None:
        """Periodically log metrics."""
        while self._running:
            await asyncio.sleep(10)
            if not self._running:
                break
            try:
                m = await self.snapshot()
                logger.info(
                    f"[MONITOR] "
                    f"pages={m.pages_crawled} "
                    f"skip={m.pages_skipped} "
                    f"fail={m.pages_failed} "
                    f"queue={m.queue_size} "
                    f"workers={m.active_workers}/{m.max_workers} "
                    f"speed={m.pages_per_sec_rolling:.1f} p/s (rolling) "
                    f"{m.pages_per_sec_overall:.1f} p/s (overall) "
                    f"avg={m.avg_page_ms:.0f}ms "
                    f"p95={m.p95_page_ms:.0f}ms "
                    f"words/pg={m.avg_words_per_page:.0f} "
                    f"elapsed={m.elapsed_sec:.0f}s"
                )
                if self._progress_callback:
                    try:
                        self._progress_callback(m)
                    except Exception:
                        pass
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"[MONITOR] Reporter error: {e}")

    def format_summary(self, metrics: CrawlMetrics) -> str:
        """Format a human-readable summary string."""
        lines = [
            "=" * 65,
            "  CRAWL PERFORMANCE SUMMARY",
            "=" * 65,
            f"  Pages crawled:       {metrics.pages_crawled}",
            f"  Pages skipped:       {metrics.pages_skipped} (empty/cookie/loading)",
            f"  Pages failed:        {metrics.pages_failed}",
            f"  Pages retried:       {metrics.pages_retried}",
            f"  Total enqueued:      {metrics.total_enqueued}",
            "-" * 65,
            f"  Overall speed:       {metrics.pages_per_sec_overall:.2f} pages/sec",
            f"  Rolling speed (30s): {metrics.pages_per_sec_rolling:.2f} pages/sec",
            f"  Avg page time:       {metrics.avg_page_ms:.0f} ms",
            f"  Avg navigate time:   {metrics.avg_navigate_ms:.0f} ms",
            f"  Avg extract time:    {metrics.avg_extract_ms:.0f} ms",
            f"  P95 page time:       {metrics.p95_page_ms:.0f} ms",
            "-" * 65,
            f"  Queue peak:          {metrics.queue_peak}",
            f"  Workers:             {metrics.max_workers}",
            f"  Links discovered:    {metrics.total_links_discovered}",
            "-" * 65,
            f"  Total words:         {metrics.total_words:,}",
            f"  Avg words/page:      {metrics.avg_words_per_page:.0f}",
            f"  Total bytes:         {metrics.total_bytes:,}",
            "-" * 65,
            f"  Elapsed time:        {metrics.elapsed_sec:.1f} s",
            f"  Stop reason:         {metrics.stop_reason}",
            "=" * 65,
        ]
        return "\n".join(lines)
