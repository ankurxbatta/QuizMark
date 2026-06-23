"use client";
import { Download, FileCheck, FileText } from "lucide-react";
import { API_URL } from "@/lib/api";

export default function ExportPage() {
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
          <h2 className="font-semibold text-gray-800">All Questions — Printable PDF</h2>
          <p className="text-sm text-gray-500 mt-1">
            Open a print-ready view of every question, then use your browser&apos;s “Save as PDF”.
            Tip: to export a specific set, build a Quiz and use its Export buttons.
          </p>
          <div className="flex flex-wrap gap-3 mt-4">
            <button onClick={() => window.open(`/print?all=1&answers=1`, "_blank")}
              className="flex items-center gap-2 bg-blue-600 text-white px-5 py-2.5 rounded-lg text-sm font-medium hover:bg-blue-700">
              <FileCheck size={16} /> Answer key (with answers & details)
            </button>
            <button onClick={() => window.open(`/print?all=1&answers=0`, "_blank")}
              className="flex items-center gap-2 bg-white border border-gray-300 text-gray-800 px-5 py-2.5 rounded-lg text-sm font-medium hover:bg-gray-50">
              <FileText size={16} /> Blank question paper (answer space)
            </button>
          </div>
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
