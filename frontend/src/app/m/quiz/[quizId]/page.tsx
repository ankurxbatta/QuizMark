"use client";
/**
 * Mobile quiz player — the page a student lands on after scanning a quiz QR
 * code. Works like a lightweight app: sign in with their existing student
 * account, press Start (which starts their personal timer), then answer one
 * question at a time Kahoot-style. Every keystroke is drafted locally and
 * autosaved to the server, each question can be submitted on its own, and
 * when a strict timer runs out whatever was filled in is submitted
 * automatically. Responsive, so it also works fine in a desktop browser.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams } from "next/navigation";
import Cookies from "js-cookie";
import api, { API_URL } from "@/lib/api";
import MathText from "@/components/MathText";
import TableWithMath from "@/components/TableWithMath";
import {
  AlertTriangle, Check, CheckCircle, ChevronLeft, ChevronRight, Clock, Flag,
  GraduationCap, LayoutGrid, Loader2, Lock, LogIn, LogOut, Play, Send, X,
} from "lucide-react";

// ─── Types ────────────────────────────────────────────────────────────────────

interface QuestionAsset {
  kind: string;
  caption?: string;
  alt_text?: string;
  table_html?: string;
  image_id?: string;
}

interface Question {
  id: string;
  question_text: string;
  question_type: string;
  max_marks: number;
  topic_tag?: string;
  difficulty?: string;
  assets?: QuestionAsset[];
}

interface PlayerMeta {
  id: string;
  title: string;
  description?: string | null;
  question_count: number;
  time_limit_minutes?: number | null;
  timing_mode: "strict" | "easy";
}

interface Attempt {
  id: string;
  status: "in_progress" | "completed" | "expired";
  started_at: string;
  deadline_at?: string | null;
  finished_at?: string | null;
  duration_seconds?: number | null;
  late_by_seconds: number;
  draft_answers: Record<string, string>;
}

interface SubmittedLite { submission_id: string; answer_text: string; is_marked: boolean }

interface MyResult {
  id: string;
  question_id: string;
  max_marks: number;
  auto_mark: number | null;
  auto_feedback: string | null;
  override_mark: number | null;
  override_feedback: string | null;
  is_marked: boolean;
  is_flagged: boolean;
}

type Phase = "boot" | "login" | "lobby" | "playing" | "done";

// ─── Helpers (mirrors the desktop assessment page) ───────────────────────────

function extractMcqParts(text: string) {
  const toOptionMatches = (source: string, pattern: RegExp, offset = 0) =>
    Array.from(source.matchAll(pattern)).map((match) => {
      const markerOffset = match[0].search(/(?:Option\s*)?[A-D]/i);
      const index = offset + (match.index ?? 0) + Math.max(markerOffset, 0);
      return { letter: match[1].toUpperCase(), index, end: offset + (match.index ?? 0) + match[0].length };
    });

  let matches = toOptionMatches(text, /^\s*(?:Option\s*)?([A-D])[).:\-]\s*/gim);
  let stemEnd = matches[0]?.index ?? 0;
  if (matches.length === 0) {
    const label = /\b(?:Options|Choices|Answers)\s*[:\-]\s*/i.exec(text);
    if (label) {
      const offset = (label.index ?? 0) + label[0].length;
      matches = toOptionMatches(text.slice(offset), /(?:^|[^A-Za-z0-9])(?:Option\s*)?([A-D])[).:\-]\s*/gi, offset);
      stemEnd = label.index ?? 0;
    }
  }
  if (matches.length === 0) return { stem: text.trim(), options: [] as { letter: string; text: string }[] };
  const stem = text.slice(0, stemEnd).trim();
  const options: { letter: string; text: string }[] = [];
  for (let i = 0; i < matches.length; i++) {
    const start = matches[i].end;
    const end = i + 1 < matches.length ? matches[i + 1].index : text.length;
    const optionText = text.slice(start, end).trim();
    if (optionText) options.push({ letter: matches[i].letter, text: optionText });
  }
  return { stem, options };
}

function cleanFeedback(feedback: string | null): string {
  if (!feedback) return "";
  return feedback.replace(/^\[Route:[A-Z]+\|Conf:[0-9.]+\]\s*/, "").trim();
}

function fmtClock(totalSeconds: number): string {
  const s = Math.max(0, Math.floor(totalSeconds));
  const m = Math.floor(s / 60);
  return `${m}:${(s % 60).toString().padStart(2, "0")}`;
}

function fmtNum(n: number): string {
  return n % 1 === 0 ? String(n) : n.toFixed(1);
}

function parseUtc(iso: string): number {
  // Backend datetimes may arrive without a zone suffix — they are always UTC.
  return new Date(/Z$|[+-]\d\d:\d\d$/.test(iso) ? iso : `${iso}Z`).getTime();
}

function QuestionAssets({ assets }: { assets?: QuestionAsset[] }) {
  if (!assets || assets.length === 0) return null;
  return (
    <div className="space-y-3">
      {assets.map((asset, idx) => (
        <div key={idx} className="space-y-1.5">
          {asset.table_html ? (
            <TableWithMath
              html={asset.table_html}
              className="overflow-x-auto border border-slate-200 rounded-lg [&_table]:w-full [&_table]:text-sm [&_th]:border [&_th]:border-slate-200 [&_th]:bg-slate-50 [&_th]:px-2 [&_th]:py-1.5 [&_th]:text-left [&_th]:font-semibold [&_td]:border [&_td]:border-slate-200 [&_td]:px-2 [&_td]:py-1.5"
            />
          ) : asset.image_id ? (
            <img
              src={`${API_URL}/api/v1/questions/assets/${asset.image_id}?token=${Cookies.get("token") || ""}`}
              alt={asset.alt_text || "Figure"}
              className="rounded-lg border border-slate-200 max-h-64 max-w-full"
            />
          ) : null}
          {asset.caption && <p className="text-xs text-slate-400 italic">{asset.caption}</p>}
        </div>
      ))}
    </div>
  );
}

// ─── Page ─────────────────────────────────────────────────────────────────────

export default function MobileQuizPlayer() {
  const { quizId } = useParams<{ quizId: string }>();

  const [phase, setPhase] = useState<Phase>("boot");
  const [meta, setMeta] = useState<PlayerMeta | null>(null);
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  const [questions, setQuestions] = useState<Question[]>([]);
  const [submitted, setSubmitted] = useState<Record<string, SubmittedLite>>({});
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [error, setError] = useState("");

  // server-clock offset so the countdown can't be cheated by a wrong phone clock
  const serverOffsetRef = useRef(0);
  const nowServer = useCallback(() => Date.now() + serverOffsetRef.current, []);

  const localKey = `qm-player-${quizId}`;

  const signOut = () => {
    Cookies.remove("token");
    Cookies.remove("role");
    setPhase("login");
  };

  // ── Boot: figure out where the student is in the flow ──────────────────────
  const loadLobby = useCallback(async () => {
    try {
      const { data } = await api.get(`/quizzes/${quizId}/player`);
      serverOffsetRef.current = parseUtc(data.server_now) - Date.now();
      setMeta(data.quiz);
      setAttempt(data.attempt);
      setError("");
      setPhase("lobby");
    } catch (err: any) {
      const status = err?.response?.status;
      if (status === 401) setPhase("login");
      else if (status === 403) { setError("This quiz is not assigned to your account."); setPhase("login"); }
      else if (status === 404) { setError("Quiz not found — check the link with your instructor."); setPhase("login"); }
      else { setError("Could not load the quiz. Check your connection and try again."); setPhase("login"); }
    }
  }, [quizId]);

  useEffect(() => {
    if (!Cookies.get("token")) setPhase("login");
    else loadLobby();
  }, [loadLobby]);

  // ── Start / resume an attempt ───────────────────────────────────────────────
  const [starting, setStarting] = useState(false);
  const start = async () => {
    setStarting(true);
    setError("");
    try {
      const { data } = await api.post(`/quizzes/${quizId}/attempt/start`);
      serverOffsetRef.current = parseUtc(data.server_now) - Date.now();
      setMeta(data.quiz);
      setAttempt(data.attempt);
      setQuestions(data.questions);
      setSubmitted(data.submitted || {});
      // drafts: server copy, overlaid with anything newer saved on this device
      let local: Record<string, string> = {};
      try { local = JSON.parse(localStorage.getItem(localKey) || "{}"); } catch { /* fresh */ }
      const merged = { ...(data.attempt.draft_answers || {}), ...local };
      setAnswers(merged);
      if (data.attempt.status !== "in_progress") setPhase("done");
      else setPhase("playing");
    } catch (err: any) {
      setError(err?.response?.data?.detail || "Could not start the quiz. Try again.");
    } finally {
      setStarting(false);
    }
  };

  if (phase === "boot") {
    return (
      <Shell>
        <div className="flex-1 flex items-center justify-center gap-2 text-white/80 text-sm">
          <Loader2 size={18} className="animate-spin" /> Loading…
        </div>
      </Shell>
    );
  }
  if (phase === "login") return <LoginView error={error} onSuccess={loadLobby} />;
  if (phase === "lobby" && meta) {
    return (
      <LobbyView
        meta={meta} attempt={attempt} starting={starting} error={error}
        onStart={start} onResults={start} onSignOut={signOut}
      />
    );
  }
  if (phase === "playing" && meta && attempt) {
    return (
      <PlayView
        quizId={quizId} meta={meta} attempt={attempt} questions={questions}
        submitted={submitted} setSubmitted={setSubmitted}
        answers={answers} setAnswers={setAnswers}
        localKey={localKey} nowServer={nowServer}
        onDone={(a) => { setAttempt(a); setPhase("done"); }}
        onAuthLost={() => setPhase("login")}
      />
    );
  }
  if (phase === "done" && meta) {
    return <ResultsView meta={meta} attempt={attempt} questions={questions} onSignOut={signOut} />;
  }
  return null;
}

// ─── Chrome ───────────────────────────────────────────────────────────────────

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-dvh flex flex-col bg-gradient-to-b from-brand-700 via-brand-600 to-brand-800">
      {children}
    </div>
  );
}

// ─── Login ────────────────────────────────────────────────────────────────────

function LoginView({ error: outerError, onSuccess }: { error: string; onSuccess: () => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError("");
    try {
      const { data } = await api.post("/auth/login", { username, password });
      const token: string = data.access_token;
      let role = "student";
      try { role = JSON.parse(atob(token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/"))).role ?? "student"; } catch { /* default */ }
      if (role !== "student") {
        setError("Please sign in with a student account.");
        return;
      }
      Cookies.set("token", token, { expires: 1 / 48 });
      Cookies.set("role", role);
      onSuccess();
    } catch (err: any) {
      setError(err?.response?.data?.detail || "Sign-in failed. Check your username and password.");
    } finally {
      setLoading(false);
    }
  };

  return (
    <Shell>
      <div className="flex-1 flex flex-col items-center justify-center px-5 py-10">
        <div className="flex h-16 w-16 items-center justify-center rounded-3xl bg-white/15 backdrop-blur">
          <GraduationCap size={34} className="text-white" />
        </div>
        <h1 className="mt-4 text-2xl font-bold text-white tracking-tight">QuizMark</h1>
        <p className="text-sm text-brand-100 mt-1">Sign in to take your quiz</p>

        <form onSubmit={submit} className="mt-8 w-full max-w-sm bg-white rounded-3xl shadow-xl p-6 space-y-4">
          {(outerError || error) && (
            <p className="rounded-xl bg-rose-50 px-3 py-2.5 text-sm text-rose-700" role="alert">
              {error || outerError}
            </p>
          )}
          <div>
            <label htmlFor="m-user" className="block text-sm font-medium text-slate-700 mb-1.5">Username</label>
            <input
              id="m-user" autoComplete="username" autoCapitalize="none" required
              value={username} onChange={(e) => setUsername(e.target.value)}
              className="w-full rounded-xl border border-slate-300 px-4 py-3 text-base focus:outline-none focus:ring-2 focus:ring-brand-500"
            />
          </div>
          <div>
            <label htmlFor="m-pass" className="block text-sm font-medium text-slate-700 mb-1.5">Password</label>
            <input
              id="m-pass" type="password" autoComplete="current-password" required
              value={password} onChange={(e) => setPassword(e.target.value)}
              className="w-full rounded-xl border border-slate-300 px-4 py-3 text-base focus:outline-none focus:ring-2 focus:ring-brand-500"
            />
          </div>
          <button
            type="submit" disabled={loading}
            className="w-full flex items-center justify-center gap-2 rounded-xl bg-cta-500 hover:bg-cta-600 active:scale-[0.98] text-white font-semibold py-3.5 text-base transition-all disabled:opacity-60 cursor-pointer"
          >
            {loading ? <Loader2 size={18} className="animate-spin" /> : <LogIn size={18} />}
            {loading ? "Signing in…" : "Sign In"}
          </button>
          <p className="text-xs text-slate-400 text-center leading-relaxed">
            Use the same username &amp; password you registered with. No account yet? Register once on the main site.
          </p>
        </form>
      </div>
    </Shell>
  );
}

// ─── Lobby ────────────────────────────────────────────────────────────────────

function LobbyView({
  meta, attempt, starting, error, onStart, onResults, onSignOut,
}: {
  meta: PlayerMeta; attempt: Attempt | null; starting: boolean; error: string;
  onStart: () => void; onResults: () => void; onSignOut: () => void;
}) {
  const finished = attempt && attempt.status !== "in_progress";
  const inProgress = attempt?.status === "in_progress";
  return (
    <Shell>
      <div className="flex justify-end p-4">
        <button onClick={onSignOut} className="flex items-center gap-1.5 text-brand-100 hover:text-white text-sm cursor-pointer">
          <LogOut size={15} /> Sign out
        </button>
      </div>
      <div className="flex-1 flex flex-col items-center justify-center px-5 pb-14">
        <div className="w-full max-w-sm bg-white rounded-3xl shadow-xl overflow-hidden">
          <div className="bg-brand-50 px-6 pt-8 pb-6 text-center">
            <div className="mx-auto flex h-14 w-14 items-center justify-center rounded-2xl bg-brand-600 shadow-sm">
              <GraduationCap size={28} className="text-white" />
            </div>
            <h1 className="mt-4 text-xl font-bold text-slate-900 leading-snug">{meta.title}</h1>
            {meta.description && <p className="mt-1.5 text-sm text-slate-500">{meta.description}</p>}
          </div>
          <div className="px-6 py-5 space-y-4">
            <div className="flex justify-center gap-2 flex-wrap">
              <span className="inline-flex items-center gap-1.5 rounded-full bg-brand-50 text-brand-700 text-xs font-semibold px-3 py-1.5">
                {meta.question_count} question{meta.question_count !== 1 ? "s" : ""}
              </span>
              {meta.time_limit_minutes ? (
                <span className="inline-flex items-center gap-1.5 rounded-full bg-amber-50 text-amber-700 text-xs font-semibold px-3 py-1.5">
                  <Clock size={12} /> {meta.time_limit_minutes} min
                </span>
              ) : (
                <span className="inline-flex items-center gap-1.5 rounded-full bg-slate-100 text-slate-500 text-xs font-semibold px-3 py-1.5">
                  No time limit
                </span>
              )}
            </div>

            {meta.time_limit_minutes ? (
              <p className="text-xs text-slate-500 text-center leading-relaxed">
                {meta.timing_mode === "strict"
                  ? "Your timer starts when you press Start. When it ends, whatever you've filled in is submitted automatically."
                  : "Your timer starts when you press Start. Going over time is allowed, but it's recorded and your instructor may deduct marks."}
              </p>
            ) : null}

            {error && <p className="rounded-xl bg-rose-50 px-3 py-2.5 text-sm text-rose-700">{error}</p>}

            {finished ? (
              <button
                onClick={onResults} disabled={starting}
                className="w-full flex items-center justify-center gap-2 rounded-xl bg-brand-600 hover:bg-brand-700 active:scale-[0.98] text-white font-semibold py-4 text-base transition-all cursor-pointer disabled:opacity-60"
              >
                {starting ? <Loader2 size={18} className="animate-spin" /> : <CheckCircle size={18} />}
                {attempt?.status === "expired" ? "Time ended — view my answers" : "View my results"}
              </button>
            ) : (
              <button
                onClick={onStart} disabled={starting}
                className="w-full flex items-center justify-center gap-2 rounded-xl bg-cta-500 hover:bg-cta-600 active:scale-[0.98] text-white font-bold py-4 text-lg transition-all cursor-pointer disabled:opacity-60 shadow-lg shadow-cta-500/30"
              >
                {starting ? <Loader2 size={20} className="animate-spin" /> : <Play size={20} className="fill-current" />}
                {starting ? "Starting…" : inProgress ? "Resume quiz" : "Start"}
              </button>
            )}
            {inProgress && !finished && (
              <p className="text-xs text-amber-600 text-center">
                You already started — your timer is running. Your saved answers are waiting for you.
              </p>
            )}
          </div>
        </div>
      </div>
    </Shell>
  );
}

// ─── Play ─────────────────────────────────────────────────────────────────────

function PlayView({
  quizId, meta, attempt, questions, submitted, setSubmitted, answers, setAnswers,
  localKey, nowServer, onDone, onAuthLost,
}: {
  quizId: string;
  meta: PlayerMeta;
  attempt: Attempt;
  questions: Question[];
  submitted: Record<string, SubmittedLite>;
  setSubmitted: React.Dispatch<React.SetStateAction<Record<string, SubmittedLite>>>;
  answers: Record<string, string>;
  setAnswers: React.Dispatch<React.SetStateAction<Record<string, string>>>;
  localKey: string;
  nowServer: () => number;
  onDone: (attempt: Attempt) => void;
  onAuthLost: () => void;
}) {
  const deadlineMs = attempt.deadline_at ? parseUtc(attempt.deadline_at) : null;
  const strict = meta.timing_mode !== "easy";

  const firstOpen = questions.findIndex((q) => !submitted[q.id]);
  const [idx, setIdx] = useState(firstOpen === -1 ? 0 : firstOpen);
  const [gridOpen, setGridOpen] = useState(false);
  const [confirmFinish, setConfirmFinish] = useState(false);
  const [finishing, setFinishing] = useState(false);
  const [timeUp, setTimeUp] = useState(false); // strict: overlay while auto-submitting
  const [remaining, setRemaining] = useState<number | null>(
    deadlineMs ? (deadlineMs - nowServer()) / 1000 : null
  );
  const [submittingQ, setSubmittingQ] = useState<string | null>(null);
  const [toast, setToast] = useState("");

  // Refs so the timeout handler and unload flush always see current state.
  const answersRef = useRef(answers);
  answersRef.current = answers;
  const submittedRef = useRef(submitted);
  submittedRef.current = submitted;
  const timeUpFiredRef = useRef(false);
  const dirtyRef = useRef<Set<string>>(new Set());

  // ── Draft persistence: localStorage immediately, server on a debounce ─────
  const syncTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const flushDrafts = useCallback(async () => {
    if (dirtyRef.current.size === 0) return;
    const dirty = [...dirtyRef.current];
    dirtyRef.current.clear();
    const payload: Record<string, string> = {};
    for (const qid of dirty) payload[qid] = answersRef.current[qid] ?? "";
    try {
      await api.put(`/quizzes/${quizId}/attempt/draft`, { answers: payload });
    } catch (err: any) {
      if (err?.response?.status === 401) onAuthLost();
      // otherwise re-mark dirty so the next flush retries
      else for (const qid of dirty) dirtyRef.current.add(qid);
    }
  }, [quizId, onAuthLost]);

  const setAnswer = (qid: string, text: string) => {
    setAnswers((prev) => {
      const next = { ...prev, [qid]: text };
      try { localStorage.setItem(localKey, JSON.stringify(next)); } catch { /* storage full */ }
      return next;
    });
    dirtyRef.current.add(qid);
    if (syncTimerRef.current) clearTimeout(syncTimerRef.current);
    syncTimerRef.current = setTimeout(flushDrafts, 2500);
  };

  // Flush on tab hide / close so a pocketed phone doesn't lose its draft.
  useEffect(() => {
    const onHide = () => {
      if (document.visibilityState === "hidden" && dirtyRef.current.size > 0) {
        const payload: Record<string, string> = {};
        for (const qid of dirtyRef.current) payload[qid] = answersRef.current[qid] ?? "";
        dirtyRef.current.clear();
        fetch(`${API_URL}/api/v1/quizzes/${quizId}/attempt/draft`, {
          method: "PUT",
          keepalive: true,
          headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${Cookies.get("token") || ""}`,
          },
          body: JSON.stringify({ answers: payload }),
        }).catch(() => { /* best effort */ });
      }
    };
    document.addEventListener("visibilitychange", onHide);
    return () => document.removeEventListener("visibilitychange", onHide);
  }, [quizId]);

  useEffect(() => () => { if (syncTimerRef.current) clearTimeout(syncTimerRef.current); }, []);

  // ── Submit a single question ───────────────────────────────────────────────
  const submitOne = useCallback(async (q: Question, opts: { advance?: boolean } = {}): Promise<boolean> => {
    const text = (answersRef.current[q.id] ?? "").trim();
    if (!text || submittedRef.current[q.id]) return !!submittedRef.current[q.id];
    setSubmittingQ(q.id);
    try {
      const res = await api.post("/submissions/", {
        question_id: q.id, answer_text: text, quiz_id: quizId,
      }, { timeout: 20000 });
      setSubmitted((prev) => ({
        ...prev,
        [q.id]: { submission_id: res.data.id, answer_text: text, is_marked: false },
      }));
      if (opts.advance) {
        const qs = questions;
        const next = qs.findIndex((other, i) => i > qs.indexOf(q) && !submittedRef.current[other.id] && other.id !== q.id);
        setTimeout(() => { if (next !== -1) setIdx(next); }, 450);
      }
      return true;
    } catch (err: any) {
      const status = err?.response?.status;
      if (status === 409) {
        // already in — treat as submitted
        setSubmitted((prev) => ({ ...prev, [q.id]: { submission_id: "", answer_text: text, is_marked: false } }));
        return true;
      }
      if (status === 410) {
        // strict deadline hit server-side — trigger the time-up flow
        setToast("Time is up — submitting what you have…");
        void handleTimeUp();
        return false;
      }
      if (status === 401) { onAuthLost(); return false; }
      setToast(err?.response?.data?.detail || "Couldn't submit — check your connection and try again.");
      setTimeout(() => setToast(""), 4000);
      return false;
    } finally {
      setSubmittingQ(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [quizId, questions]);

  // ── Finish (manual or automatic) ───────────────────────────────────────────
  const finish = useCallback(async (auto: boolean) => {
    setFinishing(true);
    if (syncTimerRef.current) clearTimeout(syncTimerRef.current);
    await flushDrafts();
    // submit every non-empty, not-yet-submitted answer
    for (const q of questions) {
      const text = (answersRef.current[q.id] ?? "").trim();
      if (text && !submittedRef.current[q.id]) {
        await submitOne(q);
      }
    }
    try {
      const { data } = await api.post(`/quizzes/${quizId}/attempt/finish`);
      try { localStorage.removeItem(localKey); } catch { /* fine */ }
      onDone(data);
    } catch (err: any) {
      if (err?.response?.status === 401) onAuthLost();
      else if (!auto) {
        setToast("Couldn't finish — check your connection and try again.");
        setTimeout(() => setToast(""), 4000);
      }
    } finally {
      setFinishing(false);
    }
  }, [flushDrafts, questions, submitOne, quizId, localKey, onDone, onAuthLost]);

  const handleTimeUp = useCallback(async () => {
    if (timeUpFiredRef.current) return;
    timeUpFiredRef.current = true;
    setTimeUp(true);
    setGridOpen(false);
    setConfirmFinish(false);
    await finish(true);
  }, [finish]);

  // ── Countdown ──────────────────────────────────────────────────────────────
  useEffect(() => {
    if (deadlineMs === null) return;
    const tick = () => {
      const left = (deadlineMs - nowServer()) / 1000;
      setRemaining(left);
      if (left <= 0 && strict) void handleTimeUp();
    };
    tick();
    const iv = setInterval(tick, 500);
    return () => clearInterval(iv);
  }, [deadlineMs, strict, nowServer, handleTimeUp]);

  const overtime = remaining !== null && remaining <= 0;
  const lowTime = remaining !== null && remaining > 0 && remaining <= 60;

  const q = questions[idx];
  const submittedCount = questions.filter((x) => submitted[x.id]).length;
  const draftCount = questions.filter(
    (x) => !submitted[x.id] && (answers[x.id] ?? "").trim().length > 0
  ).length;

  if (!q) {
    return (
      <Shell>
        <div className="flex-1 flex items-center justify-center text-white/80 text-sm">
          This quiz has no questions yet.
        </div>
      </Shell>
    );
  }

  const locked = !!submitted[q.id];
  const currentText = locked ? submitted[q.id].answer_text : (answers[q.id] ?? "");

  return (
    <div className="min-h-dvh flex flex-col bg-slate-100">
      {/* ── Sticky header: progress + timer ── */}
      <header className="sticky top-0 z-30 bg-brand-700 text-white shadow-md">
        <div className="flex items-center justify-between px-4 py-3 gap-3">
          <span className="text-sm font-semibold whitespace-nowrap">
            Q{idx + 1} <span className="text-brand-200 font-normal">/ {questions.length}</span>
          </span>
          <div className="flex-1 h-1.5 bg-brand-800/60 rounded-full overflow-hidden">
            <div
              className="h-full bg-cta-500 rounded-full transition-all duration-500"
              style={{ width: `${(submittedCount / Math.max(questions.length, 1)) * 100}%` }}
            />
          </div>
          {remaining !== null && (
            <span
              className={`flex items-center gap-1 rounded-full px-2.5 py-1 text-sm font-bold tabular-nums whitespace-nowrap ${
                overtime ? "bg-rose-500 animate-pulse" : lowTime ? "bg-amber-500 text-amber-950" : "bg-brand-800/70"
              }`}
            >
              <Clock size={13} />
              {overtime ? `+${fmtClock(-remaining)}` : fmtClock(remaining)}
            </span>
          )}
        </div>
        {/* easy-mode overtime warning banner */}
        {overtime && !strict && (
          <div className="flex items-start gap-2 bg-amber-400 text-amber-950 px-4 py-2.5 text-xs font-medium leading-snug">
            <AlertTriangle size={14} className="shrink-0 mt-0.5" />
            Time is up! You can keep going, but your instructor can see the extra
            time and may deduct marks. Finish as soon as you can.
          </div>
        )}
      </header>

      {/* ── Question card ── */}
      <main className="flex-1 px-4 py-5 pb-40 max-w-lg w-full mx-auto">
        <div key={q.id} className="animate-[qslide_.25s_ease-out] space-y-4">
          <div className="flex items-center gap-2 flex-wrap">
            {q.topic_tag && (
              <span className="rounded-full bg-white text-slate-500 border border-slate-200 text-xs px-2.5 py-1">{q.topic_tag}</span>
            )}
            {q.difficulty && (
              <span className={`rounded-full text-xs px-2.5 py-1 font-medium ${
                q.difficulty === "hard" ? "bg-rose-50 text-rose-600"
                : q.difficulty === "medium" ? "bg-amber-50 text-amber-600"
                : "bg-emerald-50 text-emerald-600"
              }`}>{q.difficulty}</span>
            )}
            <span className="ml-auto text-xs text-slate-400 font-medium">
              {q.max_marks} mark{q.max_marks !== 1 ? "s" : ""}
            </span>
          </div>

          <div className="bg-white rounded-3xl shadow-sm p-5 space-y-4">
            <QuestionAssets assets={q.assets} />
            <QuestionInput
              question={q}
              value={currentText}
              locked={locked}
              onChange={(text) => setAnswer(q.id, text)}
            />
            {locked && (
              <div className="flex items-center gap-2 rounded-xl bg-emerald-50 text-emerald-700 px-4 py-3 text-sm font-medium">
                <Lock size={15} /> Answer submitted — it&apos;s with your instructor.
              </div>
            )}
          </div>

          {/* per-question actions */}
          {!locked && (
            <div className="grid grid-cols-2 gap-3">
              <button
                onClick={() => setIdx((i) => Math.min(i + 1, questions.length - 1))}
                className="flex items-center justify-center gap-2 rounded-2xl bg-white border border-slate-200 text-slate-600 font-semibold py-3.5 active:scale-[0.98] transition-all cursor-pointer"
              >
                <Flag size={16} /> Review later
              </button>
              <button
                onClick={() => submitOne(q, { advance: true })}
                disabled={!currentText.trim() || submittingQ === q.id}
                className="flex items-center justify-center gap-2 rounded-2xl bg-cta-500 hover:bg-cta-600 text-white font-bold py-3.5 active:scale-[0.98] transition-all disabled:opacity-50 disabled:cursor-default cursor-pointer shadow-md shadow-cta-500/25"
              >
                {submittingQ === q.id ? <Loader2 size={16} className="animate-spin" /> : <Send size={16} />}
                Submit answer
              </button>
            </div>
          )}
          {!locked && (
            <p className="text-xs text-slate-400 text-center leading-relaxed">
              Submit locks this answer in. “Review later” keeps it as a draft —
              drafts are saved automatically and also count if time runs out.
            </p>
          )}
        </div>
      </main>

      {/* ── Bottom navigation ── */}
      <nav className="fixed bottom-0 inset-x-0 z-30 bg-white border-t border-slate-200 px-4 py-3 pb-[max(0.75rem,env(safe-area-inset-bottom))]">
        <div className="max-w-lg mx-auto flex items-center gap-3">
          <button
            onClick={() => setIdx((i) => Math.max(i - 1, 0))} disabled={idx === 0}
            aria-label="Previous question"
            className="flex h-12 w-12 items-center justify-center rounded-xl border border-slate-200 text-slate-600 disabled:opacity-30 active:scale-95 transition-all cursor-pointer"
          >
            <ChevronLeft size={20} />
          </button>
          <button
            onClick={() => setGridOpen(true)}
            className="flex-1 flex items-center justify-center gap-2 h-12 rounded-xl bg-slate-100 text-slate-700 text-sm font-semibold active:scale-[0.98] transition-all cursor-pointer"
          >
            <LayoutGrid size={16} />
            {submittedCount}/{questions.length} submitted{draftCount > 0 ? ` · ${draftCount} draft${draftCount !== 1 ? "s" : ""}` : ""}
          </button>
          {idx === questions.length - 1 || submittedCount === questions.length ? (
            <button
              onClick={() => setConfirmFinish(true)}
              className="h-12 px-4 rounded-xl bg-brand-600 text-white text-sm font-bold active:scale-95 transition-all cursor-pointer"
            >
              Finish
            </button>
          ) : (
            <button
              onClick={() => setIdx((i) => Math.min(i + 1, questions.length - 1))}
              aria-label="Next question"
              className="flex h-12 w-12 items-center justify-center rounded-xl border border-slate-200 text-slate-600 active:scale-95 transition-all cursor-pointer"
            >
              <ChevronRight size={20} />
            </button>
          )}
        </div>
      </nav>

      {/* toast */}
      {toast && (
        <div className="fixed bottom-24 inset-x-4 z-40 max-w-lg mx-auto rounded-xl bg-slate-900 text-white text-sm px-4 py-3 shadow-lg">
          {toast}
        </div>
      )}

      {/* ── Question grid sheet ── */}
      {gridOpen && (
        <Sheet onClose={() => setGridOpen(false)} title="Questions">
          <div className="grid grid-cols-5 gap-2.5">
            {questions.map((question, i) => {
              const isSub = !!submitted[question.id];
              const isDraft = !isSub && (answers[question.id] ?? "").trim().length > 0;
              return (
                <button
                  key={question.id}
                  onClick={() => { setIdx(i); setGridOpen(false); }}
                  className={`h-12 rounded-xl text-sm font-bold transition-all active:scale-95 cursor-pointer ${
                    isSub ? "bg-emerald-500 text-white"
                    : isDraft ? "bg-amber-400 text-amber-950"
                    : "bg-slate-100 text-slate-500"
                  } ${i === idx ? "ring-2 ring-brand-600 ring-offset-2" : ""}`}
                >
                  {isSub ? <Check size={16} className="mx-auto" /> : i + 1}
                </button>
              );
            })}
          </div>
          <div className="flex items-center justify-center gap-4 mt-4 text-xs text-slate-500">
            <span className="flex items-center gap-1.5"><span className="w-3 h-3 rounded bg-emerald-500 inline-block" /> Submitted</span>
            <span className="flex items-center gap-1.5"><span className="w-3 h-3 rounded bg-amber-400 inline-block" /> Draft</span>
            <span className="flex items-center gap-1.5"><span className="w-3 h-3 rounded bg-slate-200 inline-block" /> Empty</span>
          </div>
          <button
            onClick={() => { setGridOpen(false); setConfirmFinish(true); }}
            className="mt-5 w-full rounded-xl bg-brand-600 text-white font-bold py-3.5 active:scale-[0.98] transition-all cursor-pointer"
          >
            Finish quiz
          </button>
        </Sheet>
      )}

      {/* ── Finish confirmation ── */}
      {confirmFinish && (
        <Sheet onClose={() => finishing ? null : setConfirmFinish(false)} title="Finish quiz?">
          <div className="space-y-4">
            <p className="text-sm text-slate-600 leading-relaxed">
              {draftCount > 0
                ? `${draftCount} draft answer${draftCount !== 1 ? "s" : ""} will be submitted along with your ${submittedCount} already-submitted answer${submittedCount !== 1 ? "s" : ""}.`
                : `You've submitted ${submittedCount} of ${questions.length} questions.`}
              {questions.length - submittedCount - draftCount > 0 && (
                <span className="text-amber-600 font-medium">
                  {" "}{questions.length - submittedCount - draftCount} question{questions.length - submittedCount - draftCount !== 1 ? "s are" : " is"} unanswered.
                </span>
              )}
            </p>
            <div className="grid grid-cols-2 gap-3">
              <button
                onClick={() => setConfirmFinish(false)} disabled={finishing}
                className="rounded-xl border border-slate-200 text-slate-600 font-semibold py-3.5 active:scale-[0.98] transition-all cursor-pointer disabled:opacity-50"
              >
                Keep going
              </button>
              <button
                onClick={() => finish(false)} disabled={finishing}
                className="flex items-center justify-center gap-2 rounded-xl bg-cta-500 text-white font-bold py-3.5 active:scale-[0.98] transition-all cursor-pointer disabled:opacity-60"
              >
                {finishing && <Loader2 size={16} className="animate-spin" />}
                {finishing ? "Submitting…" : "Finish"}
              </button>
            </div>
          </div>
        </Sheet>
      )}

      {/* ── Strict time-up overlay ── */}
      {timeUp && (
        <div className="fixed inset-0 z-50 bg-brand-900/90 backdrop-blur-sm flex flex-col items-center justify-center px-8 text-center">
          <Clock size={44} className="text-white mb-4" />
          <h2 className="text-2xl font-bold text-white">Time&apos;s up!</h2>
          <p className="mt-2 text-brand-100 text-sm leading-relaxed">
            Submitting everything you filled in — don&apos;t close this page…
          </p>
          <Loader2 size={22} className="animate-spin text-white mt-6" />
        </div>
      )}

    </div>
  );
}

// ─── Question input (MCQ / true-false / free text) ────────────────────────────

function QuestionInput({
  question, value, locked, onChange,
}: {
  question: Question; value: string; locked: boolean; onChange: (text: string) => void;
}) {
  if (question.question_type === "mcq") {
    const { stem, options } = extractMcqParts(question.question_text);
    if (options.length > 0) {
      return (
        <div className="space-y-4">
          <MathText text={stem || question.question_text} className="text-base font-semibold text-slate-900 leading-relaxed block" />
          <div className="space-y-2.5">
            {options.map(({ letter, text }) => {
              const on = value === letter;
              return (
                <button
                  key={letter} type="button" disabled={locked}
                  onClick={() => onChange(letter)}
                  className={`w-full flex items-start gap-3 rounded-2xl border-2 px-4 py-3.5 text-left transition-all active:scale-[0.99] ${
                    on ? "border-brand-500 bg-brand-50" : "border-slate-200 bg-white"
                  } ${locked ? "opacity-70" : "cursor-pointer hover:border-slate-300"}`}
                >
                  <span className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-lg text-sm font-bold ${
                    on ? "bg-brand-600 text-white" : "bg-slate-100 text-slate-500"
                  }`}>{letter}</span>
                  <span className="text-sm text-slate-700 pt-1"><MathText text={text} /></span>
                </button>
              );
            })}
          </div>
        </div>
      );
    }
  }

  if (question.question_type === "true_false") {
    return (
      <div className="space-y-4">
        <MathText text={question.question_text} className="text-base font-semibold text-slate-900 leading-relaxed block" />
        <div className="grid grid-cols-2 gap-3">
          {(["True", "False"] as const).map((opt) => {
            const on = value === opt;
            return (
              <button
                key={opt} type="button" disabled={locked}
                onClick={() => onChange(opt)}
                className={`rounded-2xl border-2 py-4 text-base font-bold transition-all active:scale-[0.98] ${
                  on
                    ? opt === "True"
                      ? "border-emerald-500 bg-emerald-50 text-emerald-700"
                      : "border-rose-400 bg-rose-50 text-rose-600"
                    : "border-slate-200 bg-white text-slate-600"
                } ${locked ? "opacity-70" : "cursor-pointer"}`}
              >
                {opt}
              </button>
            );
          })}
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <MathText text={question.question_text} className="text-base font-semibold text-slate-900 leading-relaxed block" />
      <textarea
        rows={5}
        placeholder="Type your answer…"
        value={value}
        readOnly={locked}
        onChange={(e) => onChange(e.target.value)}
        className={`w-full rounded-2xl border-2 border-slate-200 px-4 py-3 text-base focus:outline-none focus:border-brand-500 resize-y ${locked ? "bg-slate-50 text-slate-500" : ""}`}
      />
    </div>
  );
}

// ─── Bottom sheet ─────────────────────────────────────────────────────────────

function Sheet({ children, title, onClose }: { children: React.ReactNode; title: string; onClose: () => void }) {
  return (
    <div className="fixed inset-0 z-40 flex items-end justify-center bg-slate-900/50" onClick={onClose}>
      <div
        onClick={(e) => e.stopPropagation()}
        className="w-full max-w-lg bg-white rounded-t-3xl px-5 pt-4 pb-[max(1.25rem,env(safe-area-inset-bottom))] animate-[sheetup_.22s_ease-out]"
      >
        <div className="mx-auto h-1 w-10 rounded-full bg-slate-200 mb-3" />
        <div className="flex items-center justify-between mb-4">
          <h2 className="font-bold text-slate-900">{title}</h2>
          <button onClick={onClose} className="text-slate-400 cursor-pointer p-1"><X size={18} /></button>
        </div>
        {children}
      </div>
    </div>
  );
}

// ─── Results ──────────────────────────────────────────────────────────────────

function ResultsView({
  meta, attempt, questions, onSignOut,
}: {
  meta: PlayerMeta; attempt: Attempt | null; questions: Question[]; onSignOut: () => void;
}) {
  const [results, setResults] = useState<MyResult[]>([]);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const attemptsRef = useRef(0);

  const qids = useMemo(() => new Set(questions.map((q) => q.id)), [questions]);

  useEffect(() => {
    let iv: ReturnType<typeof setInterval> | null = null;
    const fetchResults = async () => {
      try {
        const res = await api.get<MyResult[]>("/submissions/my");
        const mine = res.data.filter((s) => qids.size === 0 || qids.has(s.question_id));
        setResults(mine);
        attemptsRef.current += 1;
        if ((mine.length > 0 && mine.every((s) => s.is_marked)) || attemptsRef.current >= 40) {
          if (iv) clearInterval(iv);
        }
      } catch { /* transient */ }
    };
    fetchResults();
    iv = setInterval(fetchResults, 2500);
    return () => { if (iv) clearInterval(iv); };
  }, [qids]);

  const marked = results.filter((s) => s.is_marked);
  const allMarked = results.length > 0 && marked.length === results.length;
  const earned = marked.reduce((acc, s) => acc + (s.override_mark ?? s.auto_mark ?? 0), 0);
  const total = results.reduce((acc, s) => acc + (s.max_marks ?? 0), 0);
  const qById = useMemo(() => Object.fromEntries(questions.map((q) => [q.id, q])), [questions]);

  return (
    <Shell>
      <div className="flex-1 flex flex-col items-center px-4 py-8 gap-4 overflow-y-auto">
        <div className="w-full max-w-sm bg-white rounded-3xl shadow-xl p-6 text-center">
          <p className="text-xs font-semibold text-slate-400 uppercase tracking-wide">{meta.title}</p>
          {attempt?.status === "expired" && (
            <p className="mt-2 inline-flex items-center gap-1.5 rounded-full bg-rose-50 text-rose-600 text-xs font-semibold px-3 py-1.5">
              <Clock size={12} /> Time ran out — your filled-in answers were submitted
            </p>
          )}
          {allMarked ? (
            <>
              <p className="mt-3 text-5xl font-bold text-slate-900">
                {fmtNum(earned)}<span className="text-2xl text-slate-400 font-normal">/{fmtNum(total)}</span>
              </p>
              <p className="text-sm text-slate-400 mt-1">
                {total > 0 ? Math.round((earned / total) * 100) : 0}%
              </p>
            </>
          ) : (
            <div className="mt-4 flex items-center justify-center gap-2 text-sm text-slate-500">
              <Loader2 size={15} className="animate-spin" />
              {results.length === 0 ? "Collecting your answers…" : `Marking… ${marked.length}/${results.length} done`}
            </div>
          )}
          {attempt?.duration_seconds != null && (
            <p className="mt-3 text-xs text-slate-400">
              Time taken: {fmtClock(attempt.duration_seconds)}
              {attempt.late_by_seconds > 0 && (
                <span className="text-amber-600"> (+{fmtClock(attempt.late_by_seconds)} over)</span>
              )}
            </p>
          )}
        </div>

        <div className="w-full max-w-sm space-y-2.5 pb-6">
          {results.map((r, i) => {
            const mark = r.override_mark ?? r.auto_mark;
            const feedback = cleanFeedback(r.override_feedback ?? r.auto_feedback);
            const open = !!expanded[r.id];
            const question = qById[r.question_id];
            return (
              <div key={r.id} className="bg-white rounded-2xl shadow-sm overflow-hidden">
                <button
                  onClick={() => setExpanded((e) => ({ ...e, [r.id]: !open }))}
                  className="w-full flex items-center justify-between px-4 py-3.5 text-left cursor-pointer"
                >
                  <span className="text-sm font-semibold text-slate-700">Question {i + 1}</span>
                  {r.is_marked && mark !== null ? (
                    <span className={`text-sm font-bold ${
                      r.max_marks > 0 && mark / r.max_marks >= 0.75 ? "text-emerald-600"
                      : r.max_marks > 0 && mark / r.max_marks >= 0.5 ? "text-amber-600" : "text-rose-500"
                    }`}>{fmtNum(mark)}/{r.max_marks}</span>
                  ) : (
                    <span className="flex items-center gap-1.5 text-xs text-slate-400">
                      <Loader2 size={11} className="animate-spin" /> marking
                    </span>
                  )}
                </button>
                {open && (
                  <div className="border-t border-slate-100 px-4 py-3 space-y-2 bg-slate-50">
                    {question && (
                      <MathText text={question.question_text} className="text-xs text-slate-500 block" />
                    )}
                    {feedback && <MathText text={feedback} className="text-sm text-slate-700 leading-relaxed block" />}
                    {r.is_flagged && (
                      <p className="flex items-center gap-1.5 text-xs text-amber-600">
                        <AlertTriangle size={12} /> Under instructor review — this mark may change.
                      </p>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>

        <button
          onClick={onSignOut}
          className="mb-6 flex items-center gap-2 text-brand-100 hover:text-white text-sm font-medium cursor-pointer"
        >
          <LogOut size={15} /> Sign out
        </button>
      </div>
    </Shell>
  );
}
