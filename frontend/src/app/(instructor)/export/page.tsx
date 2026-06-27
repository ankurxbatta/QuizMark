"use client";
import { useEffect, useState } from "react";
import { Download, FileCheck, FileText } from "lucide-react";
import api, { API_URL } from "@/lib/api";

interface Quiz { id: string; title: string; question_count: number; }

export default function ExportPage() {
  const [quizzes, setQuizzes] = useState<Quiz[]>([]);
  const [selected, setSelected] = useState<string>("all"); // "all" or a quiz id

  useEffect(() => {
    api.get<Quiz[]>("/quizzes/").then(({ data }) => setQuizzes(data)).catch(() => setQuizzes([]));
  }, []);

  const scopeParam = selected === "all" ? "all=1" : `quiz=${selected}`;
  const selectedQuiz = quizzes.find((q) => q.id === selected);

  const exports = [
    {
      title: "Marks & Feedback Export",
      description: "CSV with Student ID, Question ID, Mark, Max Mark, Feedback, Override Flag, and Timestamp for every submission.",
      url: `${API_URL}/api/v1/export/marks`,
      filename: "marks_export.csv",
    },
    {
      title: "Full Audit Log Export",
      description: "CSV of all marking events, overrides, and login activity with timestamps.",
      url: `${API_URL}/api/v1/export/audit`,
      filename: "audit_log.csv",
    },
  ];

  return (
    <div className="bg-gray-50">
      <header className="bg-white border-b px-8 py-4 shadow-sm">
        <h1 className="text-xl font-bold text-blue-700">Export Data</h1>
        <p className="text-xs text-gray-400 mt-0.5">Download question papers, answer keys, results and audit logs</p>
      </header>
      <div className="max-w-3xl mx-auto px-8 py-10 space-y-5">
        {/* Question paper / answer key (print to PDF) */}
        <div className="bg-white rounded-xl border shadow-sm p-6">
          <h2 className="font-semibold text-gray-800">Question Paper — Printable PDF</h2>
          <p className="text-sm text-gray-500 mt-1">
            Pick a quiz (or all questions), open a print-ready view, then use your browser&apos;s “Save as PDF”.
            Build quizzes on the Quizzes page.
          </p>

          <div className="mt-4">
            <label className="block text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1">
              What to export
            </label>
            <select
              value={selected}
              onChange={(e) => setSelected(e.target.value)}
              className="w-full sm:w-96 border border-gray-300 rounded-lg px-3 py-2 text-sm bg-white focus:ring-2 focus:ring-blue-500 focus:outline-none"
            >
              <option value="all">All questions (entire question bank)</option>
              {quizzes.length > 0 && <optgroup label="Quizzes">
                {quizzes.map((q) => (
                  <option key={q.id} value={q.id}>
                    {q.title} ({q.question_count} question{q.question_count !== 1 ? "s" : ""})
                  </option>
                ))}
              </optgroup>}
            </select>
            {quizzes.length === 0 && (
              <p className="text-xs text-gray-400 mt-1">No quizzes yet — create one on the Quizzes page to export a specific set.</p>
            )}
          </div>

          <div className="flex flex-wrap gap-3 mt-4">
            <button onClick={() => window.open(`/print?${scopeParam}&answers=1`, "_blank")}
              className="flex items-center gap-2 bg-blue-600 text-white px-5 py-2.5 rounded-lg text-sm font-medium hover:bg-blue-700">
              <FileCheck size={16} /> Answer key (with answers & details)
            </button>
            <button onClick={() => window.open(`/print?${scopeParam}&answers=0`, "_blank")}
              className="flex items-center gap-2 bg-white border border-gray-300 text-gray-800 px-5 py-2.5 rounded-lg text-sm font-medium hover:bg-gray-50">
              <FileText size={16} /> Blank question paper (answer space)
            </button>
          </div>
          <p className="text-xs text-gray-400 mt-3">
            Exporting: <span className="font-medium text-gray-600">{selected === "all" ? "All questions" : (selectedQuiz?.title || "Selected quiz")}</span>
          </p>
        </div>

        {exports.map(({ title, description, url, filename }) => (
          <div key={title} className="bg-white rounded-xl border shadow-sm p-6 flex items-center justify-between gap-6">
            <div>
              <h2 className="font-semibold text-gray-800">{title}</h2>
              <p className="text-sm text-gray-500 mt-1">{description}</p>
            </div>
            <a href={url} download={filename}
              className="flex items-center gap-2 bg-blue-600 text-white px-5 py-2.5 rounded-lg text-sm font-medium hover:bg-blue-700 whitespace-nowrap">
              <Download size={16} /> Download CSV
            </a>
          </div>
        ))}
      </div>
    </div>
  );
}
