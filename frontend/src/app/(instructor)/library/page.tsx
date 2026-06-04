"use client";
import { useState, useEffect } from "react";
import { useRouter } from "next/navigation";
import api from "@/lib/api";
import {
  BookOpen, Database, Layers, Table2, FlaskConical,
  ImageIcon, Loader2, RefreshCw, CalendarDays, ChevronRight,
  Trash2, Hourglass,
} from "lucide-react";

interface BookChapter { num: number; title: string }

interface CachedBook {
  book_hash: string;
  book_id: string;
  filename: string;
  total_pages: number;
  pages_done: number;
  progress_percent: number;
  chunks_stored: number;
  status: string;
  updated_at: string | null;
}

interface Book {
  book_id: string;
  display_name: string;
  total_chunks: number;
  total_chapters: number;
  chapters: BookChapter[];
  with_tables: number;
  with_math: number;
  with_images: number;
  ingested_at: string | null;
}

function Stat({ icon: Icon, value, label, colour }: {
  icon: React.ElementType; value: number; label: string; colour: string;
}) {
  return (
    <div className={`flex items-center gap-1.5 text-xs px-2.5 py-1 rounded-full font-medium ${colour}`}>
      <Icon size={11} />
      <span>{value} {label}</span>
    </div>
  );
}

function BookCard({ book }: { book: Book }) {
  const router = useRouter();

  const ingested = book.ingested_at
    ? new Date(book.ingested_at).toLocaleDateString("en-GB", {
        day: "numeric", month: "short", year: "numeric",
      })
    : null;

  return (
    <button
      onClick={() => router.push(`/library/${encodeURIComponent(book.book_id)}`)}
      className="bg-white rounded-xl border shadow-sm p-6 text-left w-full hover:border-indigo-400 hover:shadow-md transition-all group"
    >
      <div className="flex items-start gap-3">
        <div className="w-10 h-10 bg-indigo-100 rounded-xl flex items-center justify-center shrink-0 group-hover:bg-indigo-200 transition-colors">
          <BookOpen size={20} className="text-indigo-600" />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center justify-between gap-2">
            <h3 className="font-semibold text-gray-900 leading-tight truncate">
              {book.display_name}
            </h3>
            <ChevronRight size={16} className="text-gray-400 group-hover:text-indigo-500 shrink-0 transition-colors" />
          </div>
          <p className="text-xs text-gray-400 mt-0.5 font-mono truncate">{book.book_id}</p>
          {ingested && (
            <p className="text-xs text-gray-400 mt-0.5 flex items-center gap-1">
              <CalendarDays size={10} /> Ingested {ingested}
            </p>
          )}
        </div>
      </div>

      <div className="flex flex-wrap gap-2 mt-4">
        <Stat icon={Layers}     value={book.total_chunks}   label="chunks"   colour="bg-indigo-50 text-indigo-700" />
        <Stat icon={Database}   value={book.total_chapters} label="chapters" colour="bg-slate-100 text-slate-600"  />
        {book.with_tables > 0 && <Stat icon={Table2}       value={book.with_tables} label="tables"   colour="bg-blue-50 text-blue-700"   />}
        {book.with_math   > 0 && <Stat icon={FlaskConical} value={book.with_math}   label="formulas" colour="bg-purple-50 text-purple-700" />}
        {book.with_images > 0 && <Stat icon={ImageIcon}    value={book.with_images} label="charts"   colour="bg-amber-50 text-amber-700"  />}
      </div>

      {book.chapters.length > 0 && (
        <p className="text-xs text-gray-400 mt-3 truncate">
          {book.chapters.slice(0, 4).map((c) => `Ch ${c.num}: ${c.title}`).join(" · ")}
          {book.chapters.length > 4 && ` · +${book.chapters.length - 4} more`}
        </p>
      )}
    </button>
  );
}

export default function LibraryPage() {
  const [books, setBooks]     = useState<Book[]>([]);
  const [cached, setCached]   = useState<CachedBook[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError]     = useState("");
  const [clearing, setClearing] = useState<string | null>(null);

  const load = async () => {
    setLoading(true);
    setError("");
    try {
      const [booksRes, cacheRes] = await Promise.all([
        api.get("/questions/books"),
        api.get("/questions/books/cache").catch(() => ({ data: { cached: [] } })),
      ]);
      setBooks(booksRes.data.books || []);
      setCached(cacheRes.data.cached || []);
    } catch {
      setError("Failed to load books. Make sure the backend is running.");
    } finally {
      setLoading(false);
    }
  };

  const clearCache = async (book_hash: string) => {
    if (!confirm(
      "Clear this cached ingestion?\n\n" +
      "• Deletes the resume checkpoint AND all partial chunks for this PDF.\n" +
      "• The next upload of this same PDF will start from page 1.\n\n" +
      "If you just want to hide this card, leave it — re-uploading this PDF later will pick up where it stopped."
    )) return;
    setClearing(book_hash);
    try {
      await api.delete(`/questions/books/${encodeURIComponent(book_hash)}/cache`);
      setCached(prev => prev.filter(c => c.book_hash !== book_hash));
    } catch {
      alert("Failed to clear cache.");
    } finally {
      setClearing(null);
    }
  };

  useEffect(() => { load(); }, []);

  return (
    <div className="min-h-screen bg-gray-50">
      <header className="bg-white border-b px-8 py-4 shadow-sm flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold text-indigo-700 flex items-center gap-2">
            <Database size={20} /> Book Library
          </h1>
          <p className="text-xs text-gray-400 mt-0.5">
            Click a book to view chapters and generate questions
          </p>
        </div>
        <button
          onClick={load}
          className="flex items-center gap-2 text-sm text-gray-500 hover:text-gray-700 border border-gray-200 rounded-lg px-3 py-2 hover:bg-gray-50"
        >
          <RefreshCw size={14} /> Refresh
        </button>
      </header>

      <main className="max-w-5xl mx-auto px-8 py-10 space-y-8">
        {loading ? (
          <div className="flex items-center justify-center py-24 text-gray-400">
            <Loader2 size={24} className="animate-spin mr-3" /> Loading books…
          </div>
        ) : error ? (
          <div className="bg-red-50 border border-red-200 rounded-xl px-6 py-5 text-red-700 text-sm">
            {error}
          </div>
        ) : (
          <>
            {/* Cached (in-progress) ingestions */}
            {cached.length > 0 && (
              <section className="space-y-3">
                <h2 className="text-sm font-semibold text-gray-700 flex items-center gap-2">
                  <Hourglass size={14} className="text-amber-500" />
                  Cached ingestions — re-upload the PDF to resume
                </h2>
                <div className="space-y-2">
                  {cached.map((c) => (
                    <div key={c.book_hash} className="bg-amber-50/50 border border-amber-200 rounded-xl p-4 flex items-center gap-4">
                      <div className="flex-1 min-w-0">
                        <p className="font-medium text-gray-800 truncate">{c.filename || c.book_id}</p>
                        <p className="text-xs text-gray-500 mt-0.5">
                          {c.pages_done} / {c.total_pages} pages read · {c.chunks_stored} chunks stored
                        </p>
                        <div className="w-full bg-amber-100 rounded-full h-1.5 mt-2">
                          <div
                            className="bg-amber-500 h-1.5 rounded-full transition-all"
                            style={{ width: `${Math.min(c.progress_percent, 100)}%` }}
                          />
                        </div>
                      </div>
                      <button
                        onClick={() => clearCache(c.book_hash)}
                        disabled={clearing === c.book_hash}
                        className="flex items-center gap-1.5 text-xs text-red-600 hover:text-red-800 border border-red-200 hover:bg-red-50 rounded-lg px-3 py-2 disabled:opacity-50 transition-colors"
                        title="Clear cache and start from page 1 next time"
                      >
                        {clearing === c.book_hash
                          ? <Loader2 size={12} className="animate-spin" />
                          : <Trash2 size={12} />}
                        Clear cache
                      </button>
                    </div>
                  ))}
                </div>
              </section>
            )}

            {/* Books grid */}
            {books.length === 0 ? (
              <div className="text-center py-24 space-y-3">
                <BookOpen size={48} className="text-gray-300 mx-auto" />
                <p className="text-gray-500 font-medium">No books in the library yet</p>
                <p className="text-sm text-gray-400">
                  Use <strong>Add Book</strong> in the sidebar to upload a PDF textbook.
                </p>
              </div>
            ) : (
              <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
                {books.map((book) => (
                  <BookCard key={book.book_id} book={book} />
                ))}
              </div>
            )}
          </>
        )}
      </main>
    </div>
  );
}
