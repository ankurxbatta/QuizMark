"use client";
import { useState } from "react";
import { useRouter } from "next/navigation";
import api from "@/lib/api";
import Cookies from "js-cookie";
import { LogIn, UserPlus } from "lucide-react";

// Minimal JWT decoder — reads the payload without verifying signature
function decodeJwtRole(token: string): string | null {
  try {
    const payload = JSON.parse(atob(token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
    return payload.role ?? null;
  } catch {
    return null;
  }
}

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

  return (
    <div className="min-h-screen flex items-center justify-center bg-gradient-to-br from-blue-50 to-indigo-100">
      <div className="bg-white rounded-2xl shadow-xl p-10 w-full max-w-md">
        <h1 className="text-3xl font-bold text-indigo-700 mb-1">QuizMark</h1>
        <p className="text-gray-500 mb-8 text-sm">
          {mode === "register" ? "Create a student account" : "AI-Powered Quiz & Marking Platform"}
        </p>

        <form onSubmit={handleSubmit} className="space-y-5">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Username</label>
            <input
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              required
              className="w-full border border-gray-300 rounded-lg px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
            />
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Password</label>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              className="w-full border border-gray-300 rounded-lg px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
            />
          </div>

          {error && <p className="text-sm text-red-600 bg-red-50 rounded-lg px-3 py-2">{error}</p>}

          <button
            type="submit"
            disabled={loading}
            className="w-full inline-flex items-center justify-center gap-2 bg-indigo-600 hover:bg-indigo-700 text-white font-semibold py-2.5 rounded-lg transition-colors disabled:opacity-60"
          >
            {mode === "register" ? <UserPlus size={17} /> : <LogIn size={17} />}
            {loading ? (mode === "register" ? "Creating account…" : "Signing in…") : (mode === "register" ? "Register" : "Sign In")}
          </button>
        </form>

        <button
          type="button"
          onClick={() => {
            setMode(mode === "login" ? "register" : "login");
            setError("");
          }}
          className="mt-5 w-full inline-flex items-center justify-center gap-2 border border-indigo-200 text-indigo-700 font-semibold py-2.5 rounded-lg hover:bg-indigo-50 transition-colors"
        >
          {mode === "login" ? <UserPlus size={17} /> : <LogIn size={17} />}
          {mode === "login" ? "Register as Student" : "Back to Sign In"}
        </button>
      </div>
    </div>
  );
}
