"use client";
import { useEffect, useState } from "react";
import api from "@/lib/api";
import Link from "next/link";
import {
  BookOpen, Upload, CheckSquare, Flag,
  Download, Clock, Database, BarChart2, Library,
} from "lucide-react";

interface Stats {
  total_questions: number;
  pending_marking: number;
  flagged: number;
  last_backup: string | null;
}

export default function InstructorDashboard() {
  const [stats, setStats] = useState<Stats>({
    total_questions: 0,
    pending_marking: 0,
    flagged: 0,
    last_backup: null,
  });

  useEffect(() => {
    Promise.all([
      api.get("/questions/count"),
      api.get("/submissions/"),
      api.get("/marking/flagged"),
    ]).then(([qCount, subs, flagged]) => {
      const submissions = subs.data as any[];
      setStats({
        total_questions: qCount.data.total,
        pending_marking: submissions.filter((s: any) => !s.is_marked).length,
        flagged: flagged.data.length,
        last_backup: new Date().toLocaleDateString(),
      });
    }).catch(() => {});
  }, []);

  const cards = [
    { label: "Q&A Bank",        value: stats.total_questions,        icon: Database,    href: "/questions",           color: "bg-indigo-50 text-indigo-700" },
    { label: "Pending Marking", value: stats.pending_marking,        icon: Clock,       href: "/marking",             color: "bg-yellow-50 text-yellow-700" },
    { label: "Flagged Reviews", value: stats.flagged,                icon: Flag,        href: "/marking?tab=flagged", color: "bg-red-50 text-red-700" },
    { label: "Last Backup",     value: stats.last_backup || "Never", icon: CheckSquare, href: "#",                    color: "bg-green-50 text-green-700" },
  ];

  const quickActions = [
    { label: "Add Book to Library",         icon: Upload,      href: "/generate",   desc: "Upload a PDF textbook — chapters, tables, formulas and charts are extracted" },
    { label: "Browse Library & Generate",   icon: Library,     href: "/library",    desc: "Pick a book, select chapters and difficulty, generate questions" },
    { label: "Manage Q&A Bank",             icon: BookOpen,    href: "/questions",  desc: "Edit, delete, or manually create questions" },
    { label: "Review & Mark Submissions",   icon: CheckSquare, href: "/marking",    desc: "Override AI marks and review flagged answers" },
    { label: "Pipeline Analytics",          icon: BarChart2,   href: "/analytics",  desc: "Confidence distribution and marking accuracy stats" },
    { label: "Export Results",              icon: Download,    href: "/export",     desc: "Download marks and audit logs" },
  ];

  return (
    <div className="bg-gray-50 min-h-screen">
      <header className="bg-white border-b px-8 py-4 shadow-sm">
        <h1 className="text-xl font-bold text-indigo-700">Dashboard</h1>
        <p className="text-xs text-gray-400 mt-0.5">AI-powered marking · Gemini RAG pipeline</p>
      </header>

      <div className="max-w-6xl mx-auto px-8 py-10 space-y-10">
        {/* Overview cards */}
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

        {/* Quick Actions */}
        <section>
          <h2 className="text-sm font-semibold text-gray-500 uppercase tracking-wide mb-4">Quick Actions</h2>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
            {quickActions.map(({ label, icon: Icon, href, desc }) => (
              <Link key={label} href={href}
                className="bg-white rounded-xl border border-gray-200 px-5 py-4 flex items-start gap-4 hover:border-indigo-400 hover:shadow-sm transition-all">
                <span className="bg-indigo-100 text-indigo-600 p-2 rounded-lg shrink-0 mt-0.5">
                  <Icon size={18} />
                </span>
                <div>
                  <p className="font-medium text-gray-700 text-sm">{label}</p>
                  <p className="text-xs text-gray-400 mt-0.5">{desc}</p>
                </div>
              </Link>
            ))}
          </div>
        </section>
      </div>
    </div>
  );
}
