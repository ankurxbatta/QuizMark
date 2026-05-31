"use client";
import { useState, useEffect } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import Cookies from "js-cookie";
import {
  LayoutDashboard, Upload, BookOpen,
  CheckSquare, BarChart2, Download, LogOut, Database, Loader2,
} from "lucide-react";

const JOBS_LS_KEY = "active_ingest_jobs";

const NAV = [
  { label: "Dashboard",  href: "/dashboard",  icon: LayoutDashboard },
  { label: "Add Book",    href: "/generate",   icon: Upload,   showJobBadge: true },
  { label: "Library",    href: "/library",    icon: Database, showJobBadge: true },
  { label: "Questions",  href: "/questions",  icon: BookOpen },
  { label: "Marking",    href: "/marking",    icon: CheckSquare },
  { label: "Analytics",  href: "/analytics",  icon: BarChart2 },
  { label: "Export",     href: "/export",     icon: Download },
];

export default function InstructorLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const [activeJobCount, setActiveJobCount] = useState(0);

  // Poll localStorage for active job IDs every 3s so the badge stays in sync
  useEffect(() => {
    const read = () => {
      try {
        const raw = localStorage.getItem(JOBS_LS_KEY);
        const ids: string[] = raw ? JSON.parse(raw) : [];
        setActiveJobCount(Array.isArray(ids) ? ids.length : 0);
      } catch {
        setActiveJobCount(0);
      }
    };
    read();
    const interval = setInterval(read, 3000);
    return () => clearInterval(interval);
  }, []);

  const signOut = () => {
    Cookies.remove("token");
    Cookies.remove("role");
    router.push("/");
  };

  return (
    <div className="flex min-h-screen bg-gray-50">
      {/* Sidebar */}
      <aside className="w-56 bg-white border-r flex flex-col shrink-0">
        <div className="px-5 py-5 border-b">
          <span className="text-lg font-bold text-indigo-700">QuizMark</span>
          <p className="text-xs text-gray-400 mt-0.5">Instructor</p>
        </div>

        <nav className="flex-1 px-3 py-4 space-y-1">
          {NAV.map(({ label, href, icon: Icon, showJobBadge }) => {
            const active = pathname === href || (href !== "/dashboard" && pathname.startsWith(href));
            const showBadge = showJobBadge && activeJobCount > 0;
            return (
              <Link
                key={href}
                href={href}
                className={`flex items-center gap-3 px-3 py-2 rounded-lg text-sm font-medium transition-colors ${
                  active
                    ? "bg-indigo-50 text-indigo-700"
                    : "text-gray-600 hover:bg-gray-100 hover:text-gray-900"
                }`}
              >
                <Icon size={17} />
                <span className="flex-1">{label}</span>
                {showBadge && (
                  <span className="flex items-center gap-0.5 bg-blue-100 text-blue-600 text-xs font-semibold px-1.5 py-0.5 rounded-full">
                    <Loader2 size={9} className="animate-spin" />
                    {activeJobCount}
                  </span>
                )}
              </Link>
            );
          })}
        </nav>

        {/* Active jobs notice */}
        {activeJobCount > 0 && (
          <div className="mx-3 mb-3 px-3 py-2.5 bg-blue-50 border border-blue-100 rounded-xl">
            <p className="text-xs text-blue-600 font-medium flex items-center gap-1.5">
              <Loader2 size={11} className="animate-spin shrink-0" />
              {activeJobCount} job{activeJobCount > 1 ? "s" : ""} processing
            </p>
            <p className="text-xs text-blue-400 mt-0.5">Safe to navigate away</p>
          </div>
        )}

        <div className="px-3 py-4 border-t">
          <button
            onClick={signOut}
            className="flex items-center gap-3 px-3 py-2 w-full rounded-lg text-sm text-gray-500 hover:bg-red-50 hover:text-red-600 transition-colors"
          >
            <LogOut size={17} />
            Sign out
          </button>
        </div>
      </aside>

      {/* Main content */}
      <main className="flex-1 overflow-auto">
        {children}
      </main>
    </div>
  );
}
