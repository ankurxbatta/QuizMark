"use client";
import { useState } from "react";
import { useRouter } from "next/navigation";
import api from "@/lib/api";
import Cookies from "js-cookie";
import { LogIn, UserPlus, GraduationCap, Check } from "lucide-react";
import { Button } from "@/components/ui";

// Minimal JWT decoder — reads the payload without verifying signature
function decodeJwtRole(token: string): string | null {
  try {
    const payload = JSON.parse(atob(token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
    return payload.role ?? null;
  } catch {
    return null;
  }
}

const BENEFITS = [
  "Generate quizzes straight from your textbooks",
  "AI-assisted marking with consistent, fair scoring",
  "Students take assessments anywhere, anytime",
];

export default function LoginPage() {
  const router = useRouter();
  const [mode, setMode] = useState<"login" | "register">("login");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const signIn = async () => {
    const { data } = await api.post("/auth/login", { username, password });
    const token: string = data.access_token;

    const role = decodeJwtRole(token) ?? "student";

    Cookies.set("token", token, { expires: 1 / 48 }); // 30 min
    Cookies.set("role", role);
    router.push(role === "instructor" ? "/dashboard" : "/assessment");
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError("");
    try {
      if (mode === "register") {
        await api.post("/auth/register", { username, password });
      }
      await signIn();
    } catch (err: any) {
      setError(err.response?.data?.detail || (mode === "register" ? "Registration failed. Please try again." : "Login failed. Please try again."));
    } finally {
      setLoading(false);
    }
  };

  const inputClass =
    "w-full rounded-lg border border-slate-300 bg-white px-4 py-2.5 text-base text-slate-900 placeholder:text-slate-400 transition-colors duration-150 focus:outline-none focus-visible:border-brand-500 focus-visible:ring-2 focus-visible:ring-brand-500";

  return (
    <div className="min-h-screen w-full bg-slate-50 lg:grid lg:grid-cols-2">
      {/* Brand panel — hidden on small screens, shown alongside the card on desktop */}
      <aside className="hidden bg-brand-600 px-12 py-16 text-white lg:flex lg:flex-col lg:justify-center">
        <div className="mx-auto w-full max-w-md">
          <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-brand-700 shadow-sm">
            <GraduationCap size={30} className="text-white" />
          </div>
          <h1 className="mt-8 text-4xl font-bold tracking-tight">QuizMark</h1>
          <p className="mt-3 text-lg text-brand-50">
            Automated quiz generation and AI-assisted marking — from textbook to grade.
          </p>

          <ul className="mt-10 space-y-4">
            {BENEFITS.map((benefit) => (
              <li key={benefit} className="flex items-start gap-3">
                <span className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-brand-700">
                  <Check size={15} className="text-white" />
                </span>
                <span className="text-base text-brand-50">{benefit}</span>
              </li>
            ))}
          </ul>
        </div>
      </aside>

      {/* Login card column */}
      <main className="flex min-h-screen items-center justify-center px-5 py-12 lg:min-h-0">
        <div className="w-full max-w-md">
          {/* Compact brand mark for mobile, where the panel is hidden */}
          <div className="mb-8 flex flex-col items-center text-center lg:hidden">
            <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-brand-600 shadow-sm">
              <GraduationCap size={26} className="text-white" />
            </div>
            <h1 className="mt-4 text-2xl font-bold tracking-tight text-slate-900">QuizMark</h1>
            <p className="mt-1 text-sm text-slate-600">Automated Quiz &amp; Marking Platform</p>
          </div>

          <div className="rounded-2xl border border-slate-200 bg-white p-8 shadow-sm sm:p-10">
            <h2 className="text-2xl font-bold tracking-tight text-slate-900">
              {mode === "register" ? "Create your account" : "Welcome back"}
            </h2>
            <p className="mt-1 text-sm text-slate-600">
              {mode === "register"
                ? "Register as a student to start taking assessments."
                : "Sign in to continue to your workspace."}
            </p>

            <form onSubmit={handleSubmit} className="mt-8 space-y-5">
              <div>
                <label htmlFor="username" className="mb-1.5 block text-sm font-medium text-slate-700">
                  Username
                </label>
                <input
                  id="username"
                  name="username"
                  type="text"
                  autoComplete="username"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  required
                  className={inputClass}
                />
              </div>

              <div>
                <label htmlFor="password" className="mb-1.5 block text-sm font-medium text-slate-700">
                  Password
                </label>
                <input
                  id="password"
                  name="password"
                  type="password"
                  autoComplete={mode === "register" ? "new-password" : "current-password"}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  required
                  className={inputClass}
                />
              </div>

              {error && (
                <p className="rounded-lg bg-rose-50 px-3 py-2 text-sm text-rose-700" role="alert">
                  {error}
                </p>
              )}

              <Button
                type="submit"
                variant="cta"
                loading={loading}
                icon={mode === "register" ? UserPlus : LogIn}
                className="w-full py-2.5 text-base"
              >
                {loading
                  ? mode === "register"
                    ? "Creating account…"
                    : "Signing in…"
                  : mode === "register"
                  ? "Register"
                  : "Sign In"}
              </Button>
            </form>

            <div className="mt-6 flex items-center justify-center gap-1.5 text-sm">
              <span className="text-slate-600">
                {mode === "login" ? "New to QuizMark?" : "Already have an account?"}
              </span>
              <button
                type="button"
                onClick={() => {
                  setMode(mode === "login" ? "register" : "login");
                  setError("");
                }}
                className="cursor-pointer rounded font-semibold text-brand-600 transition-colors duration-150 hover:text-brand-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-1"
              >
                {mode === "login" ? "Register as Student" : "Back to Sign In"}
              </button>
            </div>
          </div>
        </div>
      </main>
    </div>
  );
}
