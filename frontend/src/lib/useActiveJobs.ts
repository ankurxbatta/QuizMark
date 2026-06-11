"use client";
import { useEffect, useState } from "react";
import api from "@/lib/api";

// Shared localStorage keys for ingestion / generation job tracking.
// "active_ingest_jobs"  — IDs of jobs still queued/processing (read by the sidebar badge)
// "known_ingest_jobs"   — all IDs the generate page knows about (incl. finished, for recovery)
export const ACTIVE_JOBS_KEY = "active_ingest_jobs";
export const KNOWN_JOBS_KEY = "known_ingest_jobs";

export interface JobLike {
  job_id: string;
  status: string;
}

export function isJobActive(status: string): boolean {
  return status !== "done" && status !== "failed";
}

function readIds(key: string): string[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = localStorage.getItem(key);
    const ids = raw ? JSON.parse(raw) : [];
    return Array.isArray(ids) ? ids : [];
  } catch {
    return [];
  }
}

function writeIds(key: string, ids: string[]) {
  if (ids.length > 0) localStorage.setItem(key, JSON.stringify(ids));
  else localStorage.removeItem(key);
}

/** IDs of jobs currently queued/processing (any page). */
export function readActiveJobIds(): string[] {
  return readIds(ACTIVE_JOBS_KEY);
}

/** All known job IDs — prefers the new key, falls back to the legacy active key. */
export function readKnownJobIds(): string[] {
  const known = readIds(KNOWN_JOBS_KEY);
  return known.length > 0 ? known : readIds(ACTIVE_JOBS_KEY);
}

/**
 * Replace both keys from a page that owns its full job list (generate page):
 * active = still-running jobs, known = every job in the list.
 */
export function syncJobsToStorage(jobs: JobLike[]) {
  writeIds(ACTIVE_JOBS_KEY, jobs.filter((j) => isJobActive(j.status)).map((j) => j.job_id));
  writeIds(KNOWN_JOBS_KEY, jobs.map((j) => j.job_id));
}

/**
 * Merge this page's jobs with IDs owned by other pages and persist the active
 * list (library book page). Returns the merged ID list so callers can keep a ref.
 */
export function mergeActiveJobIds(jobs: JobLike[], otherStoredIds: string[]): string[] {
  const thisAllIds = jobs.map((j) => j.job_id);
  const thisActiveIds = jobs.filter((j) => isJobActive(j.status)).map((j) => j.job_id);
  const otherIds = otherStoredIds.filter((id) => !thisAllIds.includes(id));
  const merged = [...new Set([...otherIds, ...thisActiveIds])];
  writeIds(ACTIVE_JOBS_KEY, merged);
  return merged;
}

interface UseActiveJobsOptions {
  /** Verify stored IDs against the API on mount and purge confirmed-gone/finished jobs. */
  verifyOnMount?: boolean;
  /** Re-read the active count from localStorage on this interval (ms). 0 disables. */
  pollIntervalMs?: number;
}

/**
 * Owns the active/known ingest-job localStorage logic shared by the sidebar,
 * generate page and library book page.
 *
 * Purge rules on verify: a job is removed only when the API confirms it is
 * gone (404) or finished (done/failed). Network/server errors keep the job —
 * a flaky connection must not wipe genuinely running jobs.
 */
export function useActiveJobs(options: UseActiveJobsOptions = {}) {
  const { verifyOnMount = false, pollIntervalMs = 0 } = options;
  const [activeJobCount, setActiveJobCount] = useState(0);

  useEffect(() => {
    let cancelled = false;

    const verify = async () => {
      const ids = readActiveJobIds();
      if (ids.length === 0) {
        if (!cancelled) setActiveJobCount(0);
        return;
      }
      const checks = await Promise.all(
        ids.map((id) =>
          api
            .get(`/questions/jobs/${id}`)
            .then((r) => ({ gone: false, job: r.data as JobLike }))
            .catch((err) => ({ gone: err?.response?.status === 404, job: null }))
        )
      );
      const stillActive = ids.filter((_, i) => {
        const check = checks[i];
        if (check.gone) return false; // confirmed 404 — stale, purge
        if (!check.job) return true; // network/server error — keep, don't purge
        return isJobActive(check.job.status);
      });
      if (cancelled) return;
      if (stillActive.length !== ids.length) writeIds(ACTIVE_JOBS_KEY, stillActive);
      setActiveJobCount(stillActive.length);
    };

    if (verifyOnMount) verify();
    else setActiveJobCount(readActiveJobIds().length);

    if (pollIntervalMs > 0) {
      const interval = setInterval(() => {
        setActiveJobCount(readActiveJobIds().length);
      }, pollIntervalMs);
      return () => {
        cancelled = true;
        clearInterval(interval);
      };
    }
    return () => {
      cancelled = true;
    };
  }, [verifyOnMount, pollIntervalMs]);

  return {
    activeJobCount,
    readActiveJobIds,
    readKnownJobIds,
    syncJobsToStorage,
    mergeActiveJobIds,
    isJobActive,
  };
}
