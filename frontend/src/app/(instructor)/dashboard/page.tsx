"use client";
import { useEffect, useState } from "react";
import api from "@/lib/api";
import Link from "next/link";
import Select from "@/components/Select";
import {
  BookOpen, Upload, CheckSquare, Flag,
  Download, Clock, Database, BarChart2, Users
} from "lucide-react";

interface Stats {
  total_questions: number;
  pending_marking: number;
  flagged: number;
  last_backup: string | null;
}

interface Question {
  id: string;
  question_text: string;
  topic_tag: string;
  assigned_student_ids?: string[];
}

interface Student {
  id: string;
  username: string;
}

export default function InstructorDashboard() {
  const [stats, setStats] = useState<Stats>({
    total_questions: 0,
    pending_marking: 0,
    flagged: 0,
    last_backup: null,
  });
  const [questions, setQuestions] = useState<Question[]>([]);
  const [students, setStudents] = useState<Student[]>([]);
  const [selectedQuestionId, setSelectedQuestionId] = useState("");
  const [selectedStudentIds, setSelectedStudentIds] = useState<string[]>([]);
  const [assignmentStatus, setAssignmentStatus] = useState("");
  const [assignmentError, setAssignmentError] = useState("");
  const [savingAssignment, setSavingAssignment] = useState(false);

  useEffect(() => {
    Promise.all([
      api.get("/questions/count"),
      api.get("/submissions/"),
      api.get("/marking/flagged"),
      api.get("/questions/"),
      api.get("/auth/students"),
    ]).then(([qCount, subs, flagged, questionResponse, studentResponse]) => {
      const submissions = subs.data as any[];
      const loadedQuestions = questionResponse.data as Question[];
      setStats({
        total_questions: qCount.data.total,
        pending_marking: submissions.filter((s: any) => !s.is_marked).length,
        flagged: flagged.data.length,
        last_backup: new Date().toLocaleDateString(),
      });
      setQuestions(loadedQuestions);
      setStudents(studentResponse.data);
      if (loadedQuestions.length > 0) {
        setSelectedQuestionId((current) => current || loadedQuestions[0].id);
        setSelectedStudentIds(loadedQuestions[0].assigned_student_ids || []);
      }
    }).catch(() => {});
  }, []);

  const selectedQuestion = questions.find((question) => question.id === selectedQuestionId);

  const changeQuestion = async (questionId: string) => {
    setSelectedQuestionId(questionId);
    setAssignmentStatus("");
    setAssignmentError("");
    const question = questions.find((item) => item.id === questionId);
    setSelectedStudentIds(question?.assigned_student_ids || []);
    if (!questionId) return;

    try {
      const { data } = await api.get(`/questions/${questionId}/assignees`);
      setSelectedStudentIds(data.student_ids || []);
    } catch (err: any) {
      setAssignmentError(err.response?.data?.detail || "Could not load assignments.");
    }
  };

  const toggleStudent = (studentId: string) => {
    setSelectedStudentIds((current) =>
      current.includes(studentId)
        ? current.filter((id) => id !== studentId)
        : [...current, studentId]
    );
  };

  const saveAssignment = async () => {
    if (!selectedQuestionId) return;
    setSavingAssignment(true);
    setAssignmentStatus("");
    setAssignmentError("");
    try {
      const { data } = await api.put(`/questions/${selectedQuestionId}/assignees`, {
        student_ids: selectedStudentIds,
      });
      setQuestions((current) =>
        current.map((question) =>
          question.id === selectedQuestionId
            ? { ...question, assigned_student_ids: data.student_ids || [] }
            : question
        )
      );
      setAssignmentStatus("Assignments saved.");
    } catch (err: any) {
      setAssignmentError(err.response?.data?.detail || "Could not save assignments.");
    } finally {
      setSavingAssignment(false);
    }
  };

  const cards = [
    { label: "Q&A Bank",        value: stats.total_questions,       icon: Database,    href: "/questions",           color: "bg-indigo-50 text-indigo-700" },
    { label: "Pending Marking", value: stats.pending_marking,       icon: Clock,       href: "/marking",             color: "bg-yellow-50 text-yellow-700" },
    { label: "Flagged Reviews", value: stats.flagged,               icon: Flag,        href: "/marking?tab=flagged", color: "bg-red-50 text-red-700" },
    { label: "Last Backup",     value: stats.last_backup || "Never",icon: CheckSquare, href: "#",                   color: "bg-green-50 text-green-700" },
  ];

  const quickActions = [
    { label: "Upload Content & Generate Questions", icon: Upload,      href: "/generate" },
    { label: "Manage Q&A Bank",                    icon: BookOpen,     href: "/questions" },
    { label: "Review & Mark Submissions",          icon: CheckSquare,  href: "/marking" },
    { label: "Pipeline Analytics",                 icon: BarChart2,    href: "/analytics" },
    { label: "Export Results",                     icon: Download,     href: "/export" },
  ];

  return (
    <div className="bg-gray-50">
      <header className="bg-white border-b px-8 py-4 shadow-sm">
        <h1 className="text-xl font-bold text-indigo-700">Dashboard</h1>
        <p className="text-xs text-gray-400 mt-0.5">Hybrid SLM + RAG + LLM auto-marking</p>
      </header>

      <div className="max-w-6xl mx-auto px-8 py-10 space-y-10">
        <section>
          <h2 className="text-sm font-semibold text-gray-500 uppercase tracking-wide mb-4">Overview</h2>
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-5">
            {cards.map(({ label, value, icon: Icon, href, color }) => (
              <Link key={label} href={href}
                className={`rounded-xl p-5 flex flex-col gap-2 shadow-sm hover:shadow-md transition-shadow ${color}`}>
                <Icon size={22} />
                <span className="text-2xl font-bold">{value}</span>
                <span className="text-sm font-medium">{label}</span>
              </Link>
            ))}
          </div>
        </section>

        <section>
          <h2 className="text-sm font-semibold text-gray-500 uppercase tracking-wide mb-4">Quick Actions</h2>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            {quickActions.map(({ label, icon: Icon, href }) => (
              <Link key={label} href={href}
                className="bg-white rounded-xl border border-gray-200 px-6 py-4 flex items-center gap-4 hover:border-indigo-400 hover:shadow-sm transition-all">
                <span className="bg-indigo-100 text-indigo-600 p-2 rounded-lg"><Icon size={20} /></span>
                <span className="font-medium text-gray-700">{label}</span>
              </Link>
            ))}
          </div>
        </section>

        <section className="bg-white rounded-xl border p-6 space-y-5">
          <div className="flex items-center justify-between gap-4">
            <div>
              <h2 className="text-sm font-semibold text-gray-700">Assign Question To Students</h2>
              <p className="text-xs text-gray-400 mt-0.5">{students.length} registered student{students.length !== 1 ? "s" : ""}</p>
            </div>
            <span className="bg-emerald-100 text-emerald-700 p-2 rounded-lg"><Users size={20} /></span>
          </div>

          {questions.length === 0 ? (
            <div className="rounded-lg border border-dashed border-gray-200 px-4 py-5 text-sm text-gray-500">
              No questions available.
              <Link href="/questions" className="ml-2 font-semibold text-indigo-600 hover:text-indigo-800">Add a question</Link>
            </div>
          ) : (
            <div className="space-y-4">
              <div>
                <label className="text-xs font-medium text-gray-500 uppercase">Question</label>
                <Select
                  value={selectedQuestionId}
                  onChange={changeQuestion}
                  options={questions.map((question) => ({
                    value: question.id,
                    label: `${ question.topic_tag ? question.topic_tag + " - " : ""}${question.question_text}`,
                  }))}
                />
                {selectedQuestion && (
                  <p className="mt-2 text-xs text-gray-400">
                    Assigned to {selectedQuestion.assigned_student_ids?.length || 0} student{(selectedQuestion.assigned_student_ids?.length || 0) !== 1 ? "s" : ""}
                  </p>
                )}
              </div>

              {students.length === 0 ? (
                <div className="rounded-lg bg-gray-50 px-4 py-3 text-sm text-gray-500">No student accounts yet.</div>
              ) : (
                <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
                  {students.map((student) => (
                    <label
                      key={student.id}
                      className={`flex items-center gap-3 rounded-lg border px-3 py-2 text-sm cursor-pointer transition-colors ${
                        selectedStudentIds.includes(student.id)
                          ? "border-indigo-300 bg-indigo-50 text-indigo-800"
                          : "border-gray-200 hover:border-gray-300 text-gray-700"
                      }`}
                    >
                      <input
                        type="checkbox"
                        checked={selectedStudentIds.includes(student.id)}
                        onChange={() => toggleStudent(student.id)}
                        className="accent-indigo-600"
                      />
                      <span className="truncate">{student.username}</span>
                    </label>
                  ))}
                </div>
              )}

              {assignmentError && <p className="text-sm text-red-600 bg-red-50 rounded-lg px-3 py-2">{assignmentError}</p>}
              {assignmentStatus && <p className="text-sm text-green-700 bg-green-50 rounded-lg px-3 py-2">{assignmentStatus}</p>}

              <button
                onClick={saveAssignment}
                disabled={savingAssignment || !selectedQuestionId}
                className="inline-flex items-center gap-2 bg-indigo-600 text-white px-5 py-2 rounded-lg text-sm font-medium hover:bg-indigo-700 disabled:opacity-60"
              >
                <CheckSquare size={16} /> {savingAssignment ? "Saving..." : "Save Assignments"}
              </button>
            </div>
          )}
        </section>

        <section className="bg-white rounded-xl border p-6 space-y-3">
          <h2 className="text-sm font-semibold text-gray-700">How the hybrid pipeline works</h2>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-4 text-sm">
            {[
              { tier: "H", color: "bg-green-100 text-green-700", title: "HIGH confidence ≥85%", desc: "SLM mark accepted directly. No LLM call. ~2s per answer." },
              { tier: "M", color: "bg-blue-100 text-blue-700",  title: "MID confidence 55–85%", desc: "RAG retrieves similar answers. Offline LLM (llama3) marks with context." },
              { tier: "L", color: "bg-amber-100 text-amber-700",title: "LOW confidence <55%",   desc: "Wide RAG retrieval. Online LLM if enabled. Always flagged for review." },
            ].map(({ tier, color, title, desc }) => (
              <div key={tier} className="flex gap-3 items-start">
                <span className={`mt-0.5 w-5 h-5 rounded-full text-xs font-bold flex items-center justify-center flex-shrink-0 ${color}`}>{tier}</span>
                <div>
                  <p className="font-medium text-gray-700">{title}</p>
                  <p className="text-gray-500 text-xs mt-0.5">{desc}</p>
                </div>
              </div>
            ))}
          </div>
        </section>
      </div>
    </div>
  );
}
