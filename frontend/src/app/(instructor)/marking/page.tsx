"use client";
import { useCallback, useEffect, useState } from "react";
import api from "@/lib/api";
import { Flag, CheckCircle } from "lucide-react";

interface Submission {
  id: string;
  student_id: string;
  question_id: string;
  question_text?: string | null;
  question_type?: string | null;
  max_marks?: number | null;
  answer_text: string;
  auto_mark: number | null;
  auto_feedback: string | null;
  override_mark: number | null;
  override_feedback: string | null;
  is_flagged: boolean;
  is_marked: boolean;
}

export default function MarkingPage() {
  const [tab, setTab] = useState<"all" | "flagged">("all");
  const [submissions, setSubmissions] = useState<Submission[]>([]);
  const [overrides, setOverrides] = useState<Record<string, { mark: string; feedback: string; reason: string }>>({});
  const [saved, setSaved] = useState<string[]>([]);
  const [saving, setSaving] = useState<string[]>([]);
  const [saveErrors, setSaveErrors] = useState<Record<string, string>>({});
  const [retrying, setRetrying] = useState<string[]>([]);
  const [retryErrors, setRetryErrors] = useState<Record<string, string>>({});
  const [retrySuccess, setRetrySuccess] = useState<Record<string, string>>({});

  const load = useCallback(() => {
    const url = tab === "flagged" ? "/submissions?flagged_only=true" : "/submissions/";
    api.get(url).then((r) => setSubmissions(r.data));
  }, [tab]);

  useEffect(() => { load(); }, [load]);

  const handleOverride = async (id: string) => {
    const o = overrides[id];
    if (!o?.mark || saving.includes(id)) return;
    setSaving((s) => [...s, id]);
    setSaveErrors((e) => ({ ...e, [id]: "" }));
    try {
      await api.put(`/marking/${id}/override`, {
        override_mark: parseFloat(o.mark),
        override_feedback: o.feedback,
        override_reason: o.reason,
      });
      setSaved((s) => [...s, id]);
      load();
    } catch (err: any) {
      const msg = err.response?.data?.detail || "Failed to save override. Please try again.";
      setSaveErrors((e) => ({ ...e, [id]: msg }));
    } finally {
      setSaving((s) => s.filter((x) => x !== id));
    }
  };

  const handleRetry = async (id: string) => {
    setRetrying((r) => [...r, id]);
    setRetryErrors((e) => ({ ...e, [id]: "" }));
    setRetrySuccess((s) => ({ ...s, [id]: "" }));
    try {
      await api.post(`/marking/${id}/retry`);
      load();
      setRetrySuccess((s) => ({ ...s, [id]: "Queued for AI marking" }));
      setTimeout(() => load(), 3000);
    } catch (err: any) {
      const msg = err.response?.data?.detail || "Retry failed. Please log in again.";
      setRetryErrors((e) => ({ ...e, [id]: msg }));
    } finally {
      setRetrying((r) => r.filter((x) => x !== id));
    }
  };

  return (
    <div className="bg-gray-50">
      <header className="bg-white border-b px-8 py-4 flex items-center gap-6 shadow-sm">
        <h1 className="text-xl font-bold text-blue-700">Marking Review</h1>
        <div className="flex gap-2">
          {(["all", "flagged"] as const).map((t) => (
            <button key={t} onClick={() => setTab(t)}
              className={`px-4 py-1.5 rounded-lg text-sm font-medium transition-colors ${
                tab === t ? "bg-blue-600 text-white" : "bg-gray-100 text-gray-600 hover:bg-gray-200"
              }`}>
              {t === "flagged" ? <><Flag size={13} className="inline mr-1" />Requires Review</> : "All Submissions"}
            </button>
          ))}
        </div>
      </header>

      <div className="max-w-5xl mx-auto px-8 py-8 space-y-5">
        {submissions.length === 0 && (
          <div className="text-center text-gray-400 py-16">No submissions found.</div>
        )}
        {submissions.map((s) => {
          const o = overrides[s.id] || { mark: "", feedback: "", reason: "" };
          const isSaved = saved.includes(s.id);
          const displayMark = s.override_mark ?? s.auto_mark;
          return (
            <div key={s.id} className={`bg-white rounded-xl border shadow-sm p-6 space-y-4 ${s.is_flagged ? "border-red-200" : ""}`}>
              <div className="flex items-start justify-between">
                <div>
                  <span className="text-xs font-medium text-gray-400 uppercase">Submission</span>
                  <p className="text-xs text-gray-500 font-mono">{s.id}</p>
                </div>
                <div className="flex items-center gap-2">
                  {s.is_flagged && <span className="flex items-center gap-1 text-xs text-red-600 bg-red-50 px-2 py-1 rounded-full"><Flag size={11} />Flagged</span>}
                  {isSaved && <span className="flex items-center gap-1 text-xs text-green-600"><CheckCircle size={13} />Saved</span>}
                </div>
              </div>

              <div className="bg-gray-50 rounded-lg p-4 space-y-3">
                <div>
                  <p className="text-xs font-medium text-gray-500 mb-1">Question</p>
                  <p className="text-sm text-gray-700">{s.question_text || "—"}</p>
                  <div className="mt-2 text-xs text-gray-500 flex flex-wrap gap-3">
                    {s.question_type && (
                      <span className="capitalize">{s.question_type.replace("_", " ")}</span>
                    )}
                    {typeof s.max_marks === "number" && <span>{s.max_marks} marks</span>}
                  </div>
                </div>
                <div>
                  <p className="text-xs font-medium text-gray-500 mb-1">Student Answer</p>
                  <p className="text-sm text-gray-700">{s.answer_text}</p>
                </div>
              </div>

              <div className="grid grid-cols-2 gap-4 text-sm">
                <div>
                  <p className="text-xs font-medium text-gray-500 mb-1">Auto Mark</p>
                  <p className="font-semibold text-gray-800">{s.auto_mark ?? "—"}</p>
                </div>
                <div>
                  <p className="text-xs font-medium text-gray-500 mb-1">Auto Feedback</p>
                  <p className="text-gray-600">{s.auto_feedback || "—"}</p>
                </div>
              </div>

              {!s.is_marked && (
                <div className="space-y-2">
                  <button
                    onClick={() => handleRetry(s.id)}
                    disabled={retrying.includes(s.id)}
                    className="text-xs text-blue-600 hover:text-blue-700 disabled:opacity-60"
                  >
                    {retrying.includes(s.id) ? "Retrying AI…" : "Retry AI Marking"}
                  </button>
                  {retrySuccess[s.id] && (
                    <p className="text-xs text-green-600">{retrySuccess[s.id]}</p>
                  )}
                  {retryErrors[s.id] && (
                    <p className="text-xs text-red-600">{retryErrors[s.id]}</p>
                  )}
                </div>
              )}

              <div className="border-t pt-4 space-y-3">
                <p className="text-xs font-semibold text-gray-500 uppercase">Instructor Override</p>
                <div className="grid grid-cols-3 gap-3">
                  <input type="number" placeholder="Override mark" value={o.mark}
                    onChange={(e) => setOverrides({ ...overrides, [s.id]: { ...o, mark: e.target.value } })}
                    className="border border-gray-300 rounded-lg px-3 py-2 text-sm" />
                  <input type="text" placeholder="Reason (optional)" value={o.reason}
                    onChange={(e) => setOverrides({ ...overrides, [s.id]: { ...o, reason: e.target.value } })}
                    className="border border-gray-300 rounded-lg px-3 py-2 text-sm" />
                  <button onClick={() => handleOverride(s.id)}
                    disabled={saving.includes(s.id)}
                    className="bg-blue-600 text-white rounded-lg px-4 py-2 text-sm font-medium hover:bg-blue-700 disabled:opacity-60">
                    {saving.includes(s.id) ? "Saving…" : "Save Override"}
                  </button>
                </div>
                {saveErrors[s.id] && (
                  <p className="text-xs text-red-600">{saveErrors[s.id]}</p>
                )}
                <textarea rows={2} placeholder="Override feedback…" value={o.feedback}
                  onChange={(e) => setOverrides({ ...overrides, [s.id]: { ...o, feedback: e.target.value } })}
                  className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
