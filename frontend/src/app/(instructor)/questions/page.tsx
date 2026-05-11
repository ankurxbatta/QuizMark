"use client";
import { useEffect, useState } from "react";
import api from "@/lib/api";
import { Trash2, Pencil, Plus } from "lucide-react";

interface Question {
  id: string;
  question_text: string;
  question_type: string;
  model_answer: string;
  rubric: string;
  max_marks: number;
  topic_tag: string;
  difficulty: string;
}

const EMPTY: Omit<Question, "id"> = {
  question_text: "", question_type: "short_answer",
  model_answer: "", rubric: "", max_marks: 5,
  topic_tag: "", difficulty: "medium",
};

export default function QuestionsPage() {
  const [questions, setQuestions] = useState<Question[]>([]);
  const [form, setForm] = useState(EMPTY);
  const [editId, setEditId] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [loading, setLoading] = useState(false);

  const load = () => api.get("/questions/").then((r) => setQuestions(r.data));
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
    const { id, ...rest } = q;
    setForm(rest); setEditId(id); setShowForm(true);
  };

  return (
    <div className="min-h-screen bg-gray-50">
      <header className="bg-white border-b px-8 py-4 flex items-center justify-between shadow-sm">
        <h1 className="text-xl font-bold text-indigo-700">Q&amp;A Bank Management</h1>
        <button onClick={() => { setForm(EMPTY); setEditId(null); setShowForm(true); }}
          className="flex items-center gap-2 bg-indigo-600 text-white px-4 py-2 rounded-lg text-sm font-medium hover:bg-indigo-700">
          <Plus size={16} /> Add Question
        </button>
      </header>

      <main className="max-w-6xl mx-auto px-8 py-8">
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

        <div className="bg-white rounded-xl border shadow-sm overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 text-gray-500 uppercase text-xs">
              <tr>
                {["Question", "Type", "Topic", "Difficulty", "Marks", "Actions"].map((h) => (
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
                  <td className="px-4 py-3 flex gap-2">
                    <button onClick={() => startEdit(q)} className="text-indigo-500 hover:text-indigo-700"><Pencil size={15} /></button>
                    <button onClick={() => del(q.id)} className="text-red-400 hover:text-red-600"><Trash2 size={15} /></button>
                  </td>
                </tr>
              ))}
              {questions.length === 0 && (
                <tr><td colSpan={6} className="px-4 py-8 text-center text-gray-400">No questions yet. Add one above.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </main>
    </div>
  );
}
