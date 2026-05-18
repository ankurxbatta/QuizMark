"use client";
import { useEffect, useState } from "react";
import api from "@/lib/api";
import { Check, Pencil, Plus, Trash2, Users, X } from "lucide-react";

interface Question {
  id: string;
  question_text: string;
  question_type: string;
  model_answer: string;
  rubric: string;
  max_marks: number;
  topic_tag: string;
  difficulty: string;
  assigned_student_ids?: string[];
}

interface Student {
  id: string;
  username: string;
}

const EMPTY: Omit<Question, "id"> = {
  question_text: "", question_type: "short_answer",
  model_answer: "", rubric: "", max_marks: 5,
  topic_tag: "", difficulty: "medium",
};

export default function QuestionsPage() {
  const [questions, setQuestions] = useState<Question[]>([]);
  const [students, setStudents] = useState<Student[]>([]);
  const [form, setForm] = useState(EMPTY);
  const [editId, setEditId] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [loading, setLoading] = useState(false);
  const [activeQuestion, setActiveQuestion] = useState<Question | null>(null);
  const [selectedStudentIds, setSelectedStudentIds] = useState<string[]>([]);
  const [assigning, setAssigning] = useState(false);
  const [visibilityError, setVisibilityError] = useState("");

  const load = async () => {
    const [questionResponse, studentResponse] = await Promise.all([
      api.get("/questions/"),
      api.get("/auth/students"),
    ]);
    setQuestions(questionResponse.data);
    setStudents(studentResponse.data);
  };
  useEffect(() => { load(); }, []);

  const save = async () => {
    setLoading(true);
    try {
      if (editId) {
        await api.put(`/questions/${editId}`, form);
      } else {
        await api.post("/questions/", form);
      }
      setForm(EMPTY); setEditId(null); setShowForm(false);
      load();
    } finally { setLoading(false); }
  };

  const del = async (id: string) => {
    if (!confirm("Delete this question?")) return;
    await api.delete(`/questions/${id}`);
    load();
  };

  const startEdit = (q: Question) => {
    const { id, assigned_student_ids, ...rest } = q;
    setForm(rest); setEditId(id); setShowForm(true);
  };

  const openVisibility = async (q: Question) => {
    setActiveQuestion(q);
    setVisibilityError("");
    setSelectedStudentIds(q.assigned_student_ids || []);
    try {
      const { data } = await api.get(`/questions/${q.id}/assignees`);
      setSelectedStudentIds(data.student_ids || []);
    } catch (err: any) {
      setVisibilityError(err.response?.data?.detail || "Could not load student visibility.");
    }
  };

  const toggleStudent = (studentId: string) => {
    setSelectedStudentIds((current) =>
      current.includes(studentId)
        ? current.filter((id) => id !== studentId)
        : [...current, studentId]
    );
  };

  const saveVisibility = async () => {
    if (!activeQuestion) return;
    setAssigning(true);
    setVisibilityError("");
    try {
      const { data } = await api.put(`/questions/${activeQuestion.id}/assignees`, {
        student_ids: selectedStudentIds,
      });
      setQuestions((current) =>
        current.map((q) =>
          q.id === activeQuestion.id
            ? { ...q, assigned_student_ids: data.student_ids || [] }
            : q
        )
      );
      setActiveQuestion(null);
    } catch (err: any) {
      setVisibilityError(err.response?.data?.detail || "Could not save student visibility.");
    } finally {
      setAssigning(false);
    }
  };

  return (
    <div className="bg-gray-50">
      <header className="bg-white border-b px-8 py-4 flex items-center justify-between shadow-sm">
        <div>
          <h1 className="text-xl font-bold text-indigo-700">Q&amp;A Bank</h1>
          <p className="text-xs text-gray-400 mt-0.5">{students.length} registered student{students.length !== 1 ? "s" : ""}</p>
        </div>
        <div className="flex items-center gap-3">
          <button onClick={() => { setForm(EMPTY); setEditId(null); setShowForm(true); }}
            className="flex items-center gap-2 bg-indigo-600 text-white px-4 py-2 rounded-lg text-sm font-medium hover:bg-indigo-700">
            <Plus size={16} /> Add Question
          </button>
        </div>
      </header>

      <div className="max-w-6xl mx-auto px-8 py-8">
        {showForm && (
          <div className="bg-white rounded-xl border border-indigo-200 shadow-sm p-6 mb-8 space-y-4">
            <h2 className="font-semibold text-gray-700">{editId ? "Edit Question" : "New Question"}</h2>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              {(["question_text", "model_answer", "rubric"] as const).map((f) => (
                <div key={f} className={f === "rubric" || f === "model_answer" ? "md:col-span-2" : ""}>
                  <label className="text-xs font-medium text-gray-500 uppercase">{f.replace(/_/g, " ")}</label>
                  <textarea rows={3} value={(form as any)[f]}
                    onChange={(e) => setForm({ ...form, [f]: e.target.value })}
                    className="w-full mt-1 border border-gray-300 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-indigo-500 focus:outline-none" />
                </div>
              ))}
              <div>
                <label className="text-xs font-medium text-gray-500 uppercase">Type</label>
                <select value={form.question_type}
                  onChange={(e) => setForm({ ...form, question_type: e.target.value })}
                  className="w-full mt-1 border border-gray-300 rounded-lg px-3 py-2 text-sm">
                  <option value="short_answer">Short Answer</option>
                  <option value="mcq">MCQ</option>
                  <option value="true_false">True / False</option>
                </select>
              </div>
              <div>
                <label className="text-xs font-medium text-gray-500 uppercase">Max Marks</label>
                <input type="number" value={form.max_marks}
                  onChange={(e) => setForm({ ...form, max_marks: parseFloat(e.target.value) })}
                  className="w-full mt-1 border border-gray-300 rounded-lg px-3 py-2 text-sm" />
              </div>
              <div>
                <label className="text-xs font-medium text-gray-500 uppercase">Topic Tag</label>
                <input type="text" value={form.topic_tag}
                  onChange={(e) => setForm({ ...form, topic_tag: e.target.value })}
                  className="w-full mt-1 border border-gray-300 rounded-lg px-3 py-2 text-sm" />
              </div>
              <div>
                <label className="text-xs font-medium text-gray-500 uppercase">Difficulty</label>
                <select value={form.difficulty}
                  onChange={(e) => setForm({ ...form, difficulty: e.target.value })}
                  className="w-full mt-1 border border-gray-300 rounded-lg px-3 py-2 text-sm">
                  <option value="easy">Easy</option>
                  <option value="medium">Medium</option>
                  <option value="hard">Hard</option>
                </select>
              </div>
            </div>
            <div className="flex gap-3">
              <button onClick={save} disabled={loading}
                className="bg-indigo-600 text-white px-5 py-2 rounded-lg text-sm font-medium hover:bg-indigo-700 disabled:opacity-60">
                {loading ? "Saving…" : editId ? "Update" : "Create"}
              </button>
              <button onClick={() => setShowForm(false)}
                className="text-gray-500 px-5 py-2 rounded-lg text-sm border hover:bg-gray-50">Cancel</button>
            </div>
          </div>
        )}

        {activeQuestion && (
          <div className="bg-white rounded-xl border border-indigo-200 shadow-sm p-6 mb-8">
            <div className="flex items-start justify-between gap-4 mb-5">
              <div>
                <h2 className="font-semibold text-gray-800">Assign Students</h2>
                <p className="text-sm text-gray-500 mt-1 line-clamp-2">{activeQuestion.question_text}</p>
              </div>
              <button
                onClick={() => setActiveQuestion(null)}
                className="text-gray-400 hover:text-gray-700"
                aria-label="Close student assignments"
              >
                <X size={18} />
              </button>
            </div>

            {students.length === 0 ? (
              <div className="text-sm text-gray-500 bg-gray-50 rounded-lg px-4 py-3">
                No students have registered yet.
              </div>
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

            {visibilityError && <p className="mt-4 text-sm text-red-600 bg-red-50 rounded-lg px-3 py-2">{visibilityError}</p>}

            <div className="flex gap-3 mt-5">
              <button
                onClick={saveVisibility}
                disabled={assigning}
                className="inline-flex items-center gap-2 bg-indigo-600 text-white px-5 py-2 rounded-lg text-sm font-medium hover:bg-indigo-700 disabled:opacity-60"
              >
                <Check size={16} /> {assigning ? "Saving…" : "Save Assignments"}
              </button>
              <button
                onClick={() => setActiveQuestion(null)}
                className="text-gray-500 px-5 py-2 rounded-lg text-sm border hover:bg-gray-50"
              >
                Cancel
              </button>
            </div>
          </div>
        )}

        <div className="bg-white rounded-xl border shadow-sm overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 text-gray-500 uppercase text-xs">
              <tr>
                {["Question", "Type", "Topic", "Difficulty", "Marks", "Assigned", "Actions"].map((h) => (
                  <th key={h} className="px-4 py-3 text-left font-medium">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {questions.map((q) => (
                <tr key={q.id} className="hover:bg-gray-50">
                  <td className="px-4 py-3 max-w-xs truncate">{q.question_text}</td>
                  <td className="px-4 py-3 capitalize">{q.question_type.replace("_", " ")}</td>
                  <td className="px-4 py-3">{q.topic_tag}</td>
                  <td className="px-4 py-3 capitalize">{q.difficulty}</td>
                  <td className="px-4 py-3">{q.max_marks}</td>
                  <td className="px-4 py-3 text-gray-500">
                    {q.assigned_student_ids?.length || 0} student{(q.assigned_student_ids?.length || 0) !== 1 ? "s" : ""}
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex flex-wrap items-center gap-2">
                      <button onClick={() => openVisibility(q)}
                        className="inline-flex items-center gap-1.5 rounded-lg border border-emerald-200 px-3 py-1.5 text-xs font-semibold text-emerald-700 hover:bg-emerald-50">
                        <Users size={14} /> Assign Students
                      </button>
                      <button onClick={() => startEdit(q)} className="text-indigo-500 hover:text-indigo-700" aria-label="Edit question"><Pencil size={15} /></button>
                      <button onClick={() => del(q.id)} className="text-red-400 hover:text-red-600" aria-label="Delete question"><Trash2 size={15} /></button>
                    </div>
                  </td>
                </tr>
              ))}
              {questions.length === 0 && (
                <tr><td colSpan={7} className="px-4 py-8 text-center text-gray-400">No questions yet. Add one above.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
