import axios from "axios";
import Cookies from "js-cookie";

export const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

const api = axios.create({
  baseURL: `${API_URL}/api/v1`,
});

api.interceptors.request.use((config) => {
  const token = Cookies.get("token");
  if (token) config.headers.Authorization = `Bearer ${token}`;
  return config;
});

// ── Silent refresh ────────────────────────────────────────────────────────────
// Long jobs (ingest, generation) outlive the 30-min token. After any successful
// call, if the token is inside the refresh window, exchange it for a fresh one
// via POST /auth/refresh. The backend caps total session age (SESSION_MAX_MINUTES),
// after which the refresh 401s and the interceptor below sends the user to login.
const REFRESH_WINDOW_MS = 10 * 60 * 1000; // refresh when < 10 min of life remains
let refreshInFlight = false;

function tokenExpiryMs(token: string): number | null {
  try {
    const payload = JSON.parse(atob(token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
    return typeof payload.exp === "number" ? payload.exp * 1000 : null;
  } catch {
    return null;
  }
}

async function maybeRefreshToken() {
  if (refreshInFlight || typeof window === "undefined") return;
  const token = Cookies.get("token");
  if (!token) return;
  const exp = tokenExpiryMs(token);
  if (exp === null || exp - Date.now() > REFRESH_WINDOW_MS) return;
  refreshInFlight = true;
  try {
    const { data } = await api.post("/auth/refresh");
    Cookies.set("token", data.access_token, { expires: 1 / 48 }); // 30 min
  } catch {
    // Session cap reached or token invalid — the 401 interceptor handles logout.
  } finally {
    refreshInFlight = false;
  }
}

// Redirect to login on 401 (expired / invalid token)
api.interceptors.response.use(
  (response) => {
    if (!response.config.url?.includes("/auth/")) void maybeRefreshToken();
    return response;
  },
  (error) => {
    if (error.response?.status === 401) {
      Cookies.remove("token");
      Cookies.remove("role");
      // The mobile quiz player (/m/…) has its own inline sign-in and must not
      // be yanked to the desktop login mid-quiz — it handles 401s itself.
      if (
        typeof window !== "undefined" &&
        window.location.pathname !== "/" &&
        !window.location.pathname.startsWith("/m/")
      ) {
        window.location.href = "/";
      }
    }
    return Promise.reject(error);
  }
);

export default api;
