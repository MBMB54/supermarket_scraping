from collections import Counter
from dataclasses import dataclass, field

NOT_FOUND_ERRORS = frozenset({"Not found in any store", "HTTP 404", "No product returned"})


@dataclass
class RunStats:
    """Counts of scraped records. Each record lands in exactly one of the buckets below.

    ok: non-empty `data`. not_found: stale ID (expected, own threshold). error: any other error.
    empty: no error but null/empty `data` (silent rate-limiting). retryable: transient failures
    that are left out of the checkpoint so a retry picks them up (counted as errors by the gate).
    """

    total: int = 0
    ok: int = 0
    not_found: int = 0
    error: int = 0
    empty: int = 0
    retryable: int = 0
    resumed_skipped: int = 0
    error_samples: Counter = field(default_factory=Counter)

    def add(self, record: dict, retryable: bool = False) -> None:
        self.total += 1
        err = record.get("error")
        if retryable:
            self.retryable += 1
        elif err in NOT_FOUND_ERRORS:
            self.not_found += 1
        elif err:
            self.error += 1
        elif record.get("data"):
            self.ok += 1
        else:
            self.empty += 1
        if err and err not in NOT_FOUND_ERRORS:
            self.error_samples[str(err)[:80]] += 1

    def to_dict(self) -> dict:
        d = {k: getattr(self, k) for k in self.__dataclass_fields__ if k != "error_samples"}
        d["error_samples"] = dict(self.error_samples.most_common(5))
        return d

    @classmethod
    def from_dict(cls, d: dict) -> RunStats:
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        known["error_samples"] = Counter(known.get("error_samples", {}))
        return cls(**known)


def rate(count: int, total: int) -> float:
    return count / total if total else 0.0


def gate_failures(
    stats: RunStats,
    *,
    max_error_rate: float,
    max_empty_rate: float,
    max_not_found_rate: float,
    fail_on_retryable: bool = False,
) -> list[str]:
    """Reasons the run must fail; empty list means it passed."""
    if stats.total == 0:
        return [] if stats.resumed_skipped else ["zero rows produced"]
    reasons = []
    if fail_on_retryable and stats.retryable:
        reasons.append(f"{stats.retryable} records still failing after retries")
    err = rate(stats.error + stats.retryable, stats.total)
    if err > max_error_rate:
        reasons.append(f"error rate {err:.1%} > {max_error_rate:.1%}")
    empty = rate(stats.empty, stats.total)
    if empty > max_empty_rate:
        reasons.append(f"empty/null payload rate {empty:.1%} > {max_empty_rate:.1%}")
    nf = rate(stats.not_found, stats.total)
    if nf > max_not_found_rate:
        reasons.append(f"not-found rate {nf:.1%} > {max_not_found_rate:.1%}")
    return reasons
