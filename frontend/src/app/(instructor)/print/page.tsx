"use client";
import { useCallback, useEffect, useState } from "react";
import Cookies from "js-cookie";
import api, { API_URL } from "@/lib/api";
import MathText from "@/components/MathText";
import TableWithMath from "@/components/TableWithMath";
import { Printer, ArrowLeft } from "lucide-react";

interface QAsset { kind: string; caption?: string; alt_text?: string; table_html?: string; image_id?: string; }
interface Question {
  id: string; question_text: string; question_type: string; model_answer: string;
  rubric?: string; max_marks?: number; topic_tag?: string; difficulty?: string;
  correct_answer?: string | null; chapter_num?: number | null; assets?: QAsset[];
}
interface Quiz { id: string; title: string; description?: string; question_ids: string[]; }

function AnswerSpace({ q }: { q: Question }) {
  if (q.question_type === "mcq" || q.question_type === "true_false") {
    return <p className="ans-line">Answer: ______</p>;
  }
  const lines = Math.min(10, Math.max(3, Math.round((q.max_marks || 2) * 1.5)));
  return <div className="ans-block">{Array.from({ length: lines }).map((_, i) => <div key={i} className="ans-rule" />)}</div>;
}

function Assets({ assets }: { assets?: QAsset[] }) {
  if (!assets?.length) return null;
  return (
    <div className="assets">
      {assets.map((a, i) => (
        <div key={i} className="asset">
          {a.table_html ? <TableWithMath html={a.table_html} />
            : a.image_id ? <img src={`${API_URL}/api/v1/questions/assets/${a.image_id}?token=${Cookies.get("token") || ""}`} alt={a.alt_text || "Figure"} style={{ maxWidth: "100%" }} />
            : null}
          {a.caption && <p className="cap">{a.caption}</p>}
        </div>
      ))}
    </div>
  );
}

// The backend caps /questions/?limit at 200, so anything that needs the full
// set (an "all questions" print, or a quiz whose ids may live past the first
// page) has to page through with skip until the data is exhausted / resolved.
const PAGE_SIZE = 200;

export default function PrintPage() {
  const [title, setTitle] = useState("Question Paper");
  const [questions, setQuestions] = useState<Question[]>([]);
  const [missingIds, setMissingIds] = useState<string[]>([]);
  const [answers, setAnswers] = useState(false);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");

  const load = useCallback(async () => {
    setLoading(true); setErr(""); setMissingIds([]);
    try {
      const params = new URLSearchParams(window.location.search);
      const withAns = params.get("answers") === "1";
      setAnswers(withAns);
      const quizId = params.get("quiz");
      let qs: Question[];
      if (quizId) {
        const { data: quiz } = await api.get<Quiz>(`/quizzes/${quizId}`);
        setTitle(quiz.title || "Quiz");
        // Resolve the quiz's question ids by paging until every id is found
        // (or the question bank is exhausted). Never silently drop ids.
        const needed = new Set(quiz.question_ids);
        const byId = new Map<string, Question>();
        for (let skip = 0; byId.size < needed.size; skip += PAGE_SIZE) {
          const { data } = await api.get<Question[]>(`/questions/?limit=${PAGE_SIZE}&skip=${skip}`);
          for (const q of data) if (needed.has(q.id)) byId.set(q.id, q);
          if (data.length < PAGE_SIZE) break; // reached the end of the bank
        }
        const resolved: Question[] = [];
        const missing: string[] = [];
        for (const id of quiz.question_ids) {
          const q = byId.get(id);
          if (q) resolved.push(q); else missing.push(id);
        }
        setMissingIds(missing);
        qs = resolved;
      } else {
        setTitle("Question Bank — All Questions");
        // Page through the entire bank rather than trusting a single capped call.
        const all: Question[] = [];
        for (let skip = 0; ; skip += PAGE_SIZE) {
          const { data } = await api.get<Question[]>(`/questions/?limit=${PAGE_SIZE}&skip=${skip}`);
          all.push(...data);
          if (data.length < PAGE_SIZE) break;
        }
        qs = all.sort((a, b) => (a.chapter_num || 0) - (b.chapter_num || 0));
      }
      setQuestions(qs);
    } catch (e: any) {
      const d = e?.response?.data?.detail;
      setErr(typeof d === "string" ? d : "Failed to load questions.");
    } finally { setLoading(false); }
  }, []);

  useEffect(() => { load(); }, [load]);

  return (
    <div className="print-root">
      <style jsx global>{`
        .print-root { position: fixed; inset: 0; z-index: 100; background: #fff; overflow: auto;
          color: #111; font-family: Georgia, "Times New Roman", serif; }
        .sheet { max-width: 820px; margin: 0 auto; padding: 40px 48px 80px; }
        .toolbar { position: sticky; top: 0; display: flex; gap: 12px; align-items: center;
          background: #f8fafc; border-bottom: 1px solid #e2e8f0; padding: 12px 24px; font-family: sans-serif; }
        .btn { display: inline-flex; align-items: center; gap: 6px; padding: 8px 14px; border-radius: 8px;
          font-size: 14px; font-weight: 600; cursor: pointer; border: 1px solid #cbd5e1; background: #fff; color: #0f172a; }
        .btn.primary { background: #2563eb; color: #fff; border-color: #2563eb; }
        .doc-head { text-align: center; border-bottom: 2px solid #111; padding-bottom: 14px; margin-bottom: 8px; }
        .doc-head h1 { font-size: 24px; margin: 0 0 6px; }
        .doc-head .meta { font-size: 13px; color: #444; }
        .nameline { display: flex; justify-content: space-between; font-size: 14px; margin: 18px 0 26px; }
        .q { margin-bottom: 26px; page-break-inside: avoid; }
        .q-head { display: flex; gap: 10px; align-items: baseline; }
        .q-num { font-weight: 700; }
        .q-marks { margin-left: auto; font-size: 13px; color: #444; white-space: nowrap; }
        .q-body { margin: 4px 0 8px; line-height: 1.5; }
        .badges { font-size: 12px; color: #555; margin: 2px 0 6px; }
        .badge { display: inline-block; border: 1px solid #cbd5e1; border-radius: 6px; padding: 1px 7px; margin-right: 6px; }
        .ans { background: #f1f5f9; border-left: 3px solid #16a34a; padding: 8px 12px; margin-top: 8px; font-size: 14px; }
        .ans .lbl { font-weight: 700; color: #166534; }
        .rubric { font-size: 12px; color: #555; margin-top: 4px; }
        .ans-block { margin-top: 8px; }
        .ans-rule { border-bottom: 1px solid #9ca3af; height: 26px; }
        .ans-line { margin-top: 10px; letter-spacing: 1px; }
        .assets { margin: 8px 0; } .asset table { border-collapse: collapse; } .cap { font-size: 12px; font-style: italic; color: #555; }
        @media print {
          @page { margin: 16mm; }
          body * { visibility: hidden !important; }
          .print-root, .print-root * { visibility: visible !important; }
          .print-root { position: absolute; inset: 0; overflow: visible; }
          .no-print { display: none !important; }
          .sheet { max-width: none; padding: 0; }
        }
      `}</style>

      <div className="toolbar no-print">
        <button className="btn" onClick={() => window.history.back()}><ArrowLeft size={16} /> Back</button>
        <button className="btn" onClick={() => { const u = new URL(window.location.href); u.searchParams.set("answers", answers ? "0" : "1"); window.location.href = u.toString(); }}>
          {answers ? "Switch to blank paper" : "Switch to answer key"}
        </button>
        <button className="btn primary" style={{ marginLeft: "auto" }} onClick={() => window.print()}><Printer size={16} /> Print / Save as PDF</button>
      </div>

      <div className="sheet">
        {loading ? <p>Loading…</p> : err ? <p style={{ color: "#b91c1c" }}>{err}</p> : (
          <>
            <div className="doc-head">
              <h1>{title}</h1>
              <div className="meta">
                {answers ? "Answer Key" : "Question Paper"} · {questions.length} question{questions.length !== 1 ? "s" : ""} · Total marks: {questions.reduce((s, q) => s + (q.max_marks || 0), 0)}
              </div>
            </div>
            {missingIds.length > 0 && (
              <div style={{ border: "1px solid #dc2626", background: "#fef2f2", color: "#991b1b",
                borderRadius: 8, padding: "10px 14px", margin: "0 0 18px", fontSize: 13, fontFamily: "sans-serif" }}>
                <b>Warning:</b> {missingIds.length} question{missingIds.length !== 1 ? "s" : ""} in this quiz could not be
                found and {missingIds.length !== 1 ? "are" : "is"} not shown below (id{missingIds.length !== 1 ? "s" : ""}: {missingIds.join(", ")}).
                {" "}They may have been deleted from the question bank.
              </div>
            )}
            {!answers && (
              <div className="nameline"><span>Name: ____________________________</span><span>Date: ____________</span><span>Score: ______</span></div>
            )}
            {questions.map((q, i) => (
              <div className="q" key={q.id}>
                <div className="q-head">
                  <span className="q-num">Q{i + 1}.</span>
                  <span className="q-marks">[{q.max_marks ?? 0} mark{(q.max_marks ?? 0) === 1 ? "" : "s"}]</span>
                </div>
                {answers && (
                  <div className="badges">
                    {q.chapter_num != null && <span className="badge">Chapter {q.chapter_num}</span>}
                    {q.difficulty && <span className="badge" style={{ textTransform: "capitalize" }}>{q.difficulty}</span>}
                    <span className="badge" style={{ textTransform: "capitalize" }}>{(q.question_type || "").replace("_", " ")}</span>
                    {q.topic_tag && <span className="badge">{q.topic_tag}</span>}
                  </div>
                )}
                <div className="q-body"><MathText text={q.question_text} /></div>
                <Assets assets={q.assets} />
                {answers ? (
                  <div className="ans">
                    <span className="lbl">Answer: </span>
                    {q.correct_answer ? <span><b>{q.correct_answer}</b>{q.model_answer ? " — " : ""}</span> : null}
                    <MathText text={q.model_answer} />
                    {q.rubric && <div className="rubric"><b>Rubric:</b> <MathText text={q.rubric} /></div>}
                  </div>
                ) : <AnswerSpace q={q} />}
              </div>
            ))}
          </>
        )}
      </div>
    </div>
  );
}
