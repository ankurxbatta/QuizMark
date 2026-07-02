"use client";
import { useEffect, useMemo, useState } from "react";
import api from "@/lib/api";
import MathText from "@/components/MathText";
import { Button, PageHeader, Card, EmptyState, Badge } from "@/components/ui";
import {
  ClipboardList, Plus, Users, Pencil, Trash2, X, Search, Check, CheckCircle2, FileCheck, FileText,
} from "lucide-react";

interface Quiz {
  id: string;
  title: string;
  description?: string | null;
  question_ids: string[];
  question_count: number;
  assigned_student_ids: string[];
}
interface Question {
  id: string;
  question_text: string;
  question_type: string;
  topic_tag?: string;
}
interface Student { id: string; username: string; }

const TYPE_LABEL: Record<string, string> = {
  mcq: "MCQ", true_false: "True/False", short_answer: "Short Answer",
};

export default function QuizzesPage() {
  const [quizzes, setQuizzes] = useState<Quiz[]>([]);
  const [questions, setQuestions] = useState<Question[]>([]);
  const [students, setStudents] = useState<Student[]>([]);
  const [loading, setLoading] = useState(true);

  // builder (create / edit)
  const [editor, setEditor] = useState<Quiz | "new" | null>(null);
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [search, setSearch] = useState("");
  const [saving, setSaving] = useState(false);

  // assignment
  const [assignQuiz, setAssignQuiz] = useState<Quiz | null>(null);
  const [assignIds, setAssignIds] = useState<Set<string>>(new Set());
  const [assignSaving, setAssignSaving] = useState(false);

  // The backend caps /questions/?limit at 200 — page through so quizzes can
  // include questions beyond the first page (same fix as the print view).
  const PAGE_SIZE = 200;
  const loadAllQuestions = async (): Promise<Question[]> => {
    const all: Question[] = [];
    for (let skip = 0; ; skip += PAGE_SIZE) {
      const { data } = await api.get<Question[]>(`/questions/?limit=${PAGE_SIZE}&skip=${skip}`);
      all.push(...data);
      if (data.length < PAGE_SIZE) break;
    }
    return all;
  };

  const load = async () => {
    try {
      const [q, qs, st] = await Promise.all([
        api.get("/quizzes/"),
        loadAllQuestions(),
        api.get("/auth/students"),
      ]);
      setQuizzes(q.data);
      setQuestions(qs);
      setStudents(st.data);
    } catch {
      // transient — keep whatever is currently shown
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => { load(); }, []);

  const openNew = () => {
    setEditor("new"); setTitle(""); setDescription(""); setPicked(new Set()); setSearch("");
  };
  const openEdit = (quiz: Quiz) => {
    setEditor(quiz); setTitle(quiz.title); setDescription(quiz.description || "");
    setPicked(new Set(quiz.question_ids)); setSearch("");
  };

  const saveQuiz = async () => {
    if (!title.trim()) return;
    setSaving(true);
    try {
      const body = { title: title.trim(), description: description.trim(), question_ids: [...picked] };
      if (editor === "new") await api.post("/quizzes/", body);
      else if (editor) await api.put(`/quizzes/${editor.id}`, body);
      setEditor(null);
      await load();
    } catch (err: any) {
      alert(err?.response?.data?.detail || "Failed to save quiz. Please try again.");
    } finally { setSaving(false); }
  };

  const del = async (quiz: Quiz) => {
    if (!confirm(`Delete quiz "${quiz.title}"? Students will no longer see it.`)) return;
    try {
      await api.delete(`/quizzes/${quiz.id}`);
    } catch (err: any) {
      alert(err?.response?.data?.detail || "Failed to delete quiz.");
    }
    load();
  };

  const openAssign = async (quiz: Quiz) => {
    setAssignQuiz(quiz);
    setAssignIds(new Set(quiz.assigned_student_ids));
    try {
      const { data } = await api.get(`/quizzes/${quiz.id}/assignees`);
      setAssignIds(new Set(data.student_ids || []));
    } catch { /* keep optimistic set */ }
  };
  const saveAssign = async () => {
    if (!assignQuiz) return;
    setAssignSaving(true);
    try {
      await api.put(`/quizzes/${assignQuiz.id}/assignees`, { student_ids: [...assignIds] });
      setAssignQuiz(null);
      await load();
    } catch (err: any) {
      alert(err?.response?.data?.detail || "Failed to save assignments. Please try again.");
    } finally { setAssignSaving(false); }
  };

  const filtered = useMemo(() => {
    const s = search.toLowerCase().trim();
    if (!s) return questions;
    return questions.filter(
      (q) => q.question_text.toLowerCase().includes(s) || (q.topic_tag || "").toLowerCase().includes(s)
    );
  }, [questions, search]);

  return (
    <div className="min-h-screen bg-slate-50">
      <PageHeader
        title="Quizzes"
        subtitle="Bundle questions into a quiz, then assign it to your students."
        actions={<Button variant="cta" icon={Plus} onClick={openNew}>New Quiz</Button>}
      />

      <div className="max-w-6xl mx-auto px-8 py-8">
        {loading ? (
          <p className="text-sm text-slate-400">Loading…</p>
        ) : quizzes.length === 0 ? (
          <Card>
            <EmptyState
              icon={ClipboardList}
              title="No quizzes yet"
              hint="Create a quiz, add questions from your bank, then assign it to students. Students see each quiz on their assessment page."
              action={<Button variant="cta" icon={Plus} onClick={openNew}>Create your first quiz</Button>}
            />
          </Card>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
            {quizzes.map((quiz) => (
              <Card key={quiz.id} className="p-5 flex flex-col">
                <div className="flex items-start justify-between gap-2">
                  <h3 className="font-semibold text-slate-900 leading-snug">{quiz.title}</h3>
                  <button onClick={() => del(quiz)} title="Delete quiz"
                    className="text-slate-300 hover:text-rose-500 transition-colors cursor-pointer shrink-0">
                    <Trash2 size={16} />
                  </button>
                </div>
                {quiz.description && <p className="text-sm text-slate-500 mt-1 line-clamp-2">{quiz.description}</p>}
                <div className="flex flex-wrap gap-2 mt-3">
                  <Badge tone="blue">{quiz.question_count} question{quiz.question_count !== 1 ? "s" : ""}</Badge>
                  <Badge tone={quiz.assigned_student_ids.length ? "green" : "slate"}>
                    <Users size={11} />
                    {quiz.assigned_student_ids.length} assigned
                  </Badge>
                </div>
                <div className="flex gap-2 mt-4 pt-4 border-t border-slate-100">
                  <Button variant="primary" icon={Users} className="flex-1" onClick={() => openAssign(quiz)}>
                    Assign
                  </Button>
                  <Button variant="ghost" icon={Pencil} onClick={() => openEdit(quiz)}>Edit</Button>
                </div>
                <div className="flex gap-2 mt-2">
                  <Button variant="ghost" icon={FileCheck} className="flex-1"
                    onClick={() => window.open(`/print?quiz=${quiz.id}&answers=1`, "_blank")}>
                    Answer key
                  </Button>
                  <Button variant="ghost" icon={FileText} className="flex-1"
                    onClick={() => window.open(`/print?quiz=${quiz.id}&answers=0`, "_blank")}>
                    Blank paper
                  </Button>
                </div>
              </Card>
            ))}
          </div>
        )}
      </div>

      {/* ── Builder modal ───────────────────────────────────────────────── */}
      {editor && (
        <Modal onClose={() => setEditor(null)} title={editor === "new" ? "New Quiz" : "Edit Quiz"} wide>
          <div className="space-y-4">
            <div>
              <label className="text-xs font-semibold text-slate-500 uppercase tracking-wide">Quiz title</label>
              <input
                value={title} onChange={(e) => setTitle(e.target.value)} autoFocus
                placeholder="e.g. Chapter 4 — Discrete Random Variables"
                className="w-full mt-1 border border-slate-300 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-brand-500 focus:outline-none"
              />
            </div>
            <div>
              <label className="text-xs font-semibold text-slate-500 uppercase tracking-wide">Description <span className="text-slate-400 normal-case font-normal">(optional)</span></label>
              <input
                value={description} onChange={(e) => setDescription(e.target.value)}
                placeholder="Shown to students above the questions"
                className="w-full mt-1 border border-slate-300 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-brand-500 focus:outline-none"
              />
            </div>

            <div>
              <div className="flex items-center justify-between mb-1.5">
                <label className="text-xs font-semibold text-slate-500 uppercase tracking-wide">
                  Questions · <span className="text-brand-600">{picked.size} selected</span>
                </label>
                <div className="relative">
                  <Search size={14} className="absolute left-2.5 top-2 text-slate-400" />
                  <input
                    value={search} onChange={(e) => setSearch(e.target.value)}
                    placeholder="Search questions…"
                    className="pl-8 pr-3 py-1.5 text-sm border border-slate-300 rounded-lg focus:ring-2 focus:ring-brand-500 focus:outline-none w-56"
                  />
                </div>
              </div>
              <div className="border border-slate-200 rounded-lg max-h-72 overflow-auto divide-y divide-slate-100">
                {filtered.length === 0 ? (
                  <p className="text-sm text-slate-400 px-4 py-6 text-center">No questions match.</p>
                ) : filtered.map((q) => {
                  const on = picked.has(q.id);
                  return (
                    <button
                      key={q.id} type="button"
                      onClick={() => setPicked((p) => { const n = new Set(p); on ? n.delete(q.id) : n.add(q.id); return n; })}
                      className={`w-full text-left flex items-start gap-3 px-4 py-2.5 transition-colors duration-150 cursor-pointer ${on ? "bg-brand-50" : "hover:bg-slate-50"}`}
                    >
                      <span className={`mt-0.5 w-4 h-4 rounded border flex items-center justify-center shrink-0 ${on ? "bg-brand-600 border-brand-600" : "border-slate-300 bg-white"}`}>
                        {on && <Check size={11} className="text-white" />}
                      </span>
                      <span className="flex-1 min-w-0">
                        <span className="text-sm text-slate-700 line-clamp-2"><MathText text={q.question_text} /></span>
                        <span className="text-xs text-slate-400">{TYPE_LABEL[q.question_type] || q.question_type}{q.topic_tag ? ` · ${q.topic_tag}` : ""}</span>
                      </span>
                    </button>
                  );
                })}
              </div>
            </div>

            <div className="flex justify-end gap-2 pt-2">
              <Button variant="ghost" onClick={() => setEditor(null)}>Cancel</Button>
              <Button variant="cta" icon={CheckCircle2} loading={saving} disabled={!title.trim()} onClick={saveQuiz}>
                {editor === "new" ? "Create quiz" : "Save changes"}
              </Button>
            </div>
          </div>
        </Modal>
      )}

      {/* ── Assignment modal ────────────────────────────────────────────── */}
      {assignQuiz && (
        <Modal onClose={() => setAssignQuiz(null)} title={`Assign "${assignQuiz.title}"`}>
          <p className="text-sm text-slate-500 mb-3">Pick the students who should see this quiz.</p>
          {students.length === 0 ? (
            <p className="text-sm text-slate-400 py-4">No students registered yet.</p>
          ) : (
            <>
              <button
                onClick={() => setAssignIds(assignIds.size === students.length ? new Set() : new Set(students.map((s) => s.id)))}
                className="text-xs font-medium text-brand-600 hover:text-brand-700 mb-2 cursor-pointer"
              >
                {assignIds.size === students.length ? "Clear all" : "Select all"}
              </button>
              <div className="border border-slate-200 rounded-lg max-h-72 overflow-auto divide-y divide-slate-100">
                {students.map((s) => {
                  const on = assignIds.has(s.id);
                  return (
                    <button key={s.id} type="button"
                      onClick={() => setAssignIds((p) => { const n = new Set(p); on ? n.delete(s.id) : n.add(s.id); return n; })}
                      className={`w-full text-left flex items-center gap-3 px-4 py-2.5 transition-colors duration-150 cursor-pointer ${on ? "bg-brand-50" : "hover:bg-slate-50"}`}
                    >
                      <span className={`w-4 h-4 rounded border flex items-center justify-center shrink-0 ${on ? "bg-brand-600 border-brand-600" : "border-slate-300 bg-white"}`}>
                        {on && <Check size={11} className="text-white" />}
                      </span>
                      <span className="text-sm text-slate-700">{s.username}</span>
                    </button>
                  );
                })}
              </div>
            </>
          )}
          <div className="flex justify-end gap-2 pt-4">
            <Button variant="ghost" onClick={() => setAssignQuiz(null)}>Cancel</Button>
            <Button variant="cta" icon={CheckCircle2} loading={assignSaving} onClick={saveAssign}>
              Assign to {assignIds.size} student{assignIds.size !== 1 ? "s" : ""}
            </Button>
          </div>
        </Modal>
      )}
    </div>
  );
}

function Modal({
  children, title, onClose, wide = false,
}: { children: React.ReactNode; title: string; onClose: () => void; wide?: boolean }) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-slate-900/40" onClick={onClose}>
      <div
        onClick={(e) => e.stopPropagation()}
        className={`bg-white rounded-2xl shadow-xl w-full ${wide ? "max-w-2xl" : "max-w-md"} max-h-[90vh] overflow-auto`}
      >
        <div className="flex items-center justify-between px-6 py-4 border-b border-slate-200 sticky top-0 bg-white">
          <h2 className="font-semibold text-slate-900">{title}</h2>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-600 cursor-pointer"><X size={18} /></button>
        </div>
        <div className="px-6 py-5">{children}</div>
      </div>
    </div>
  );
}
