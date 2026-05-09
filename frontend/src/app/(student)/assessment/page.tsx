"use client";
import { useEffect, useState } from "react";
import api from "@/lib/api";
import { CheckCircle, Clock } from "lucide-react";

interface Question {
  id: string;
  question_text: string;
  question_type: string;
  max_marks: number;
}

export default function AssessmentPage() {
  const [questions, setQuestions] = useState<Question[]>([]);
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [submitted, setSubmitted] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    api.get("/questions/").then((r) => setQuestions(r.data.slice(0, 10)));
  }, []);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    await Promise.all(
      Object.entries(answers).map(([question_id, answer_text]) =>
        api.post("/submissions/", { question_id, answer_text })
      )
    );
    setSubmitting(false);
    setSubmitted(true);
  };

  if (submitted) {
    return (
      <div className="min-h-screen bg-gray-50 flex items-center justify-center">
        <div className="bg-white rounded-2xl shadow-xl p-12 text-center max-w-md">
          <CheckCircle size={48} className="mx-auto text-green-500 mb-4" />
          <h2 className="text-2xl font-bold text-gray-800 mb-2">Submitted!</h2>
          <p className="text-gray-500">Your answers have been submitted for marking. You'll be notified when results are available.</p>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-gray-50">
      <header className="bg-white border-b px-8 py-4 flex items-center justify-between shadow-sm">
        <h1 className="text-xl font-bold text-indigo-700">Statistics Assessment</h1>
        <div className="flex items-center gap-2 text-sm text-gray-500">
          <Clock size={16} /> <span>{questions.length} questions</span>
        </div>
      </header>

      <main className="max-w-3xl mx-auto px-8 py-10">
        <form onSubmit={handleSubmit} className="space-y-6">
          {questions.map((q, i) => (
            <div key={q.id} className="bg-white rounded-xl border shadow-sm p-6 space-y-3">
              <div className="flex items-start justify-between">
                <span className="text-xs font-bold text-indigo-500 uppercase">Q{i + 1}</span>
                <span className="text-xs text-gray-400">{q.max_marks} marks</span>
              </div>
              <p className="text-gray-800 font-medium">{q.question_text}</p>
              <textarea
                rows={4}
                placeholder="Write your answer here…"
                value={answers[q.id] || ""}
                onChange={(e) => setAnswers({ ...answers, [q.id]: e.target.value })}
                required
                className="w-full border border-gray-300 rounded-lg px-4 py-3 text-sm focus:ring-2 focus:ring-indigo-500 focus:outline-none"
              />
            </div>
          ))}

          {questions.length > 0 && (
            <button type="submit" disabled={submitting}
              className="w-full bg-indigo-600 text-white py-3 rounded-xl font-semibold hover:bg-indigo-700 disabled:opacity-60 transition-colors">
              {submitting ? "Submitting…" : "Submit Assessment"}
            </button>
          )}
        </form>
      </main>
    </div>
  );
}
