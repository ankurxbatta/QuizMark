"use client";
import { useState, useRef } from "react";
import api from "@/lib/api";
import { Upload, CheckCircle, FileText, File } from "lucide-react";

interface GenerateResult {
  generated: number;
  source_file: string;
  source_pages?: number;
}

export default function GeneratePage() {
  const [file, setFile] = useState<File | null>(null);
  const [qtype, setQtype] = useState("short_answer");
  const [count, setCount] = useState(20);
  const [status, setStatus] = useState<"idle" | "loading" | "done" | "error">("idle");
  const [result, setResult] = useState<GenerateResult | null>(null);
  const [errorMsg, setErrorMsg] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  const isPDF = file?.name.toLowerCase().endsWith(".pdf");
  const isTXT = file?.name.toLowerCase().endsWith(".txt");

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!file) return;
    setStatus("loading");
    setErrorMsg("");
    const fd = new FormData();
    fd.append("file", file);
    try {
      const { data } = await api.post(
        `/questions/generate?question_type=${qtype}&count=${count}`,
        fd,
        { headers: { "Content-Type": "multipart/form-data" } }
      );
      setResult(data);
      setStatus("done");
    } catch (err: any) {
      setErrorMsg(
        err.response?.data?.detail ||
          "Generation failed. Ensure the LLM service is running."
      );
      setStatus("error");
    }
  };

  return (
    <div className="min-h-screen bg-gray-50">
      <header className="bg-white border-b px-8 py-4 shadow-sm">
        <h1 className="text-xl font-bold text-indigo-700">Generate Questions from Content</h1>
        <p className="text-sm text-gray-500 mt-0.5">
          Upload a <strong>.pdf</strong> textbook or a <strong>.txt</strong> content file — the AI will generate questions, model answers, and rubrics automatically.
        </p>
      </header>

      <main className="max-w-2xl mx-auto px-8 py-10">
        <div className="bg-white rounded-xl border shadow-sm p-8 space-y-6">
          <form onSubmit={handleSubmit} className="space-y-5">

            {/* Drop zone */}
            <div
              onClick={() => inputRef.current?.click()}
              className={`border-2 border-dashed rounded-xl p-10 text-center cursor-pointer transition-colors ${
                file
                  ? isPDF
                    ? "border-red-400 bg-red-50"
                    : "border-indigo-400 bg-indigo-50"
                  : "border-gray-300 hover:border-indigo-300"
              }`}
            >
              {file ? (
                <div className="flex flex-col items-center gap-2">
                  {isPDF ? (
                    <File size={36} className="text-red-500" />
                  ) : (
                    <FileText size={36} className="text-indigo-500" />
                  )}
                  <p className="font-medium text-gray-800">{file.name}</p>
                  <p className="text-xs text-gray-400">
                    {(file.size / 1024 / 1024).toFixed(2)} MB ·{" "}
                    {isPDF ? "PDF — text will be extracted automatically" : "Plain text"}
                  </p>
                </div>
              ) : (
                <div className="flex flex-col items-center gap-2">
                  <Upload size={36} className="text-gray-400" />
                  <p className="text-gray-500 text-sm">
                    Click to upload a <strong>.pdf</strong> or <strong>.txt</strong> file
                  </p>
                  <p className="text-xs text-gray-400">
                    PDFs up to 10 MB · first 100 pages used for generation
                  </p>
                </div>
              )}
              <input
                ref={inputRef}
                type="file"
                accept=".pdf,.txt"
                className="hidden"
                onChange={(e) => {
                  setFile(e.target.files?.[0] || null);
                  setStatus("idle");
                  setResult(null);
                }}
              />
            </div>

            {/* Options */}
            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="text-xs font-medium text-gray-500 uppercase block mb-1">
                  Question Type
                </label>
                <select
                  value={qtype}
                  onChange={(e) => setQtype(e.target.value)}
                  className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-indigo-500 focus:outline-none"
                >
                  <option value="short_answer">Short Answer</option>
                  <option value="mcq">Multiple Choice (MCQ)</option>
                  <option value="true_false">True / False</option>
                </select>
              </div>
              <div>
                <label className="text-xs font-medium text-gray-500 uppercase block mb-1">
                  Number of Questions (max 50)
                </label>
                <input
                  type="number"
                  min={1}
                  max={50}
                  value={count}
                  onChange={(e) => setCount(parseInt(e.target.value))}
                  className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-indigo-500 focus:outline-none"
                />
              </div>
            </div>

            <button
              type="submit"
              disabled={!file || status === "loading"}
              className="w-full bg-indigo-600 text-white py-2.5 rounded-lg font-medium hover:bg-indigo-700 disabled:opacity-60 transition-colors"
            >
              {status === "loading"
                ? isPDF
                  ? "Extracting PDF & generating… (may take 1–2 minutes)"
                  : "Generating questions…"
                : "Generate Questions"}
            </button>
          </form>

          {/* Success */}
          {status === "done" && result && (
            <div className="flex items-start gap-3 text-green-700 bg-green-50 rounded-xl px-5 py-4">
              <CheckCircle size={20} className="mt-0.5 flex-shrink-0" />
              <div>
                <p className="font-medium">
                  {result.generated} questions generated and added to the Q&amp;A bank.
                </p>
                <p className="text-sm text-green-600 mt-0.5">
                  Source: {result.source_file}
                  {result.source_pages ? ` · ${result.source_pages} pages` : ""}
                </p>
              </div>
            </div>
          )}

          {/* Error */}
          {status === "error" && (
            <div className="text-red-600 bg-red-50 rounded-xl px-5 py-4 text-sm">
              <strong>Error:</strong> {errorMsg}
            </div>
          )}

          {/* Info panel */}
          <div className="bg-gray-50 rounded-xl p-4 text-xs text-gray-500 space-y-1 border border-gray-100">
            <p className="font-semibold text-gray-600 text-sm mb-2">How it works</p>
            <p>📄 <strong>PDF upload:</strong> Text is extracted from up to the first 100 pages. Works with text-based PDFs (not scanned images).</p>
            <p>📝 <strong>TXT upload:</strong> The raw text is used directly.</p>
            <p>🤖 <strong>Generation:</strong> The local LLM reads your content and creates questions with model answers, rubrics, topic tags, and difficulty levels.</p>
            <p>🔒 <strong>Privacy:</strong> All processing happens on your local Ollama instance — no data leaves your infrastructure.</p>
          </div>
        </div>
      </main>
    </div>
  );
}
