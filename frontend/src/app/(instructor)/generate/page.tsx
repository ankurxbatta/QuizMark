"use client";
import { useState, useRef } from "react";
import api from "@/lib/api";
import { Upload, CheckCircle } from "lucide-react";

export default function GeneratePage() {
  const [file, setFile] = useState<File | null>(null);
  const [qtype, setQtype] = useState("short_answer");
  const [count, setCount] = useState(20);
  const [status, setStatus] = useState<"idle" | "loading" | "done" | "error">("idle");
  const [result, setResult] = useState<{ generated: number } | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!file) return;
    setStatus("loading");
    const fd = new FormData();
    fd.append("file", file);
    try {
      const { data } = await api.post(
        `/questions/generate?question_type=${qtype}&count=${count}`, fd,
        { headers: { "Content-Type": "multipart/form-data" } }
      );
      setResult(data);
      setStatus("done");
    } catch {
      setStatus("error");
    }
  };

  return (
    <div className="min-h-screen bg-gray-50">
      <header className="bg-white border-b px-8 py-4 shadow-sm">
        <h1 className="text-xl font-bold text-indigo-700">Generate Questions from Content</h1>
      </header>

      <main className="max-w-2xl mx-auto px-8 py-10">
        <div className="bg-white rounded-xl border shadow-sm p-8 space-y-6">
          <form onSubmit={handleSubmit} className="space-y-5">
            <div
              onClick={() => inputRef.current?.click()}
              className={`border-2 border-dashed rounded-xl p-10 text-center cursor-pointer transition-colors ${
                file ? "border-indigo-400 bg-indigo-50" : "border-gray-300 hover:border-indigo-300"
              }`}
            >
              <Upload size={32} className="mx-auto mb-3 text-indigo-400" />
              {file ? (
                <p className="text-indigo-700 font-medium">{file.name}</p>
              ) : (
                <p className="text-gray-500 text-sm">Click to upload a <strong>.txt</strong> content file</p>
              )}
              <input ref={inputRef} type="file" accept=".txt" className="hidden"
                onChange={(e) => setFile(e.target.files?.[0] || null)} />
            </div>

            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="text-xs font-medium text-gray-500 uppercase block mb-1">Question Type</label>
                <select value={qtype} onChange={(e) => setQtype(e.target.value)}
                  className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm">
                  <option value="short_answer">Short Answer</option>
                  <option value="mcq">Multiple Choice (MCQ)</option>
                  <option value="true_false">True / False</option>
                </select>
              </div>
              <div>
                <label className="text-xs font-medium text-gray-500 uppercase block mb-1">Number of Questions</label>
                <input type="number" min={1} max={50} value={count}
                  onChange={(e) => setCount(parseInt(e.target.value))}
                  className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
              </div>
            </div>

            <button type="submit" disabled={!file || status === "loading"}
              className="w-full bg-indigo-600 text-white py-2.5 rounded-lg font-medium hover:bg-indigo-700 disabled:opacity-60 transition-colors">
              {status === "loading" ? "Generating… (this may take a minute)" : "Generate Questions"}
            </button>
          </form>

          {status === "done" && result && (
            <div className="flex items-center gap-3 text-green-700 bg-green-50 rounded-xl px-5 py-4">
              <CheckCircle size={20} />
              <span className="font-medium">{result.generated} questions generated and added to the Q&amp;A bank.</span>
            </div>
          )}
          {status === "error" && (
            <div className="text-red-600 bg-red-50 rounded-xl px-5 py-4 text-sm">
              Something went wrong. Ensure the LLM service is running and try again.
            </div>
          )}
        </div>
      </main>
    </div>
  );
}
