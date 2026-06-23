"use client";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import Cookies from "js-cookie";
import { useActiveJobs } from "@/lib/useActiveJobs";
import {
  LayoutDashboard, Upload, BookOpen, ClipboardList,
  CheckSquare, BarChart2, Download, LogOut, Database, Loader2, GraduationCap,
} from "lucide-react";

const NAV: { label: string; href: string; icon: any; showJobBadge?: boolean; section?: string }[] = [
  { label: "Dashboard",  href: "/dashboard",  icon: LayoutDashboard },
  { label: "Add Book",   href: "/generate",   icon: Upload,   showJobBadge: true, section: "Content" },
  { label: "Library",    href: "/library",    icon: Database, showJobBadge: true },
  { label: "Questions",  href: "/questions",  icon: BookOpen },
  { label: "Quizzes",    href: "/quizzes",    icon: ClipboardList },
  { label: "Marking",    href: "/marking",    icon: CheckSquare, section: "Results" },
  { label: "Analytics",  href: "/analytics",  icon: BarChart2 },
  { label: "Export",     href: "/export",     icon: Download },
];

export default function InstructorLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  // On mount: verify stored job IDs against the API and purge confirmed-gone ones,
  // then poll localStorage every 5s (generate page manages the list).
  const { activeJobCount } = useActiveJobs({ verifyOnMount: true, pollIntervalMs: 5000 });

  const signOut = () => {
    Cookies.remove("token");
    Cookies.remove("role");
    router.push("/");
  };

  return (
    <div className="flex min-h-screen bg-slate-50">
      {/* Sidebar */}
      <aside className="w-60 bg-white border-r border-slate-200 flex flex-col shrink-0">
        <div className="px-5 py-5 border-b border-slate-200 flex items-center gap-2.5">
          <div className="w-9 h-9 rounded-xl bg-brand-600 flex items-center justify-center">
            <GraduationCap size={20} className="text-white" />
          </div>
          <div>
            <span className="text-base font-bold text-slate-900 tracking-tight">QuizMark</span>
            <p className="text-xs text-slate-400 -mt-0.5">Instructor</p>
          </div>
        </div>

        <nav className="flex-1 px-3 py-4 space-y-0.5">
          {NAV.map(({ label, href, icon: Icon, showJobBadge, section }) => {
            const active = pathname === href || (href !== "/dashboard" && pathname.startsWith(href));
            const showBadge = showJobBadge && activeJobCount > 0;
            return (
              <div key={href}>
                {section && (
                  <p className="px-3 pt-4 pb-1.5 text-[10px] font-semibold uppercase tracking-wider text-slate-400">
                    {section}
                  </p>
                )}
                <Link
                  href={href}
                  className={`flex items-center gap-3 px-3 py-2 rounded-lg text-sm font-medium transition-colors duration-150 ${
                    active
                      ? "bg-brand-50 text-brand-700"
                      : "text-slate-600 hover:bg-slate-100 hover:text-slate-900"
                  }`}
                >
                  <Icon size={18} className={active ? "text-brand-600" : ""} />
                  <span className="flex-1">{label}</span>
                  {showBadge && (
                    <span className="flex items-center gap-0.5 bg-brand-100 text-brand-700 text-xs font-semibold px-1.5 py-0.5 rounded-full">
                      <Loader2 size={9} className="animate-spin" />
                      {activeJobCount}
                    </span>
                  )}
                </Link>
              </div>
            );
          })}
        </nav>

        {/* Active jobs notice */}
        {activeJobCount > 0 && (
          <div className="mx-3 mb-3 px-3 py-2.5 bg-brand-50 border border-brand-100 rounded-xl">
            <p className="text-xs text-brand-700 font-medium flex items-center gap-1.5">
              <Loader2 size={11} className="animate-spin shrink-0" />
              {activeJobCount} job{activeJobCount > 1 ? "s" : ""} processing
            </p>
            <p className="text-xs text-brand-400 mt-0.5">Safe to navigate away</p>
          </div>
        )}

        <div className="px-3 py-4 border-t border-slate-200">
          <button
            onClick={signOut}
            className="flex items-center gap-3 px-3 py-2 w-full rounded-lg text-sm text-slate-500 hover:bg-rose-50 hover:text-rose-600 transition-colors duration-150 cursor-pointer"
          >
            <LogOut size={18} />
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
