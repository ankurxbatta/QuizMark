"use client";
import React from "react";
import { Loader2 } from "lucide-react";

// Shared, lightweight UI primitives so every page looks and behaves the same.
// Subtle 150ms transitions only — no heavy effects.

type Variant = "primary" | "cta" | "ghost" | "danger" | "subtle";

const VARIANTS: Record<Variant, string> = {
  primary: "bg-brand-600 text-white hover:bg-brand-700 shadow-sm",
  cta: "bg-cta-500 text-white hover:bg-cta-600 shadow-sm",
  ghost: "border border-slate-300 text-slate-700 bg-white hover:bg-slate-50",
  danger: "text-rose-600 hover:bg-rose-50 border border-transparent hover:border-rose-200",
  subtle: "bg-brand-50 text-brand-700 hover:bg-brand-100",
};

export function Button({
  variant = "primary",
  loading = false,
  icon: Icon,
  children,
  className = "",
  ...props
}: {
  variant?: Variant;
  loading?: boolean;
  icon?: React.ComponentType<{ size?: number | string; className?: string }>;
  children?: React.ReactNode;
} & React.ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      {...props}
      disabled={props.disabled || loading}
      className={`inline-flex items-center justify-center gap-2 rounded-lg px-4 py-2 text-sm font-semibold transition-colors duration-150 cursor-pointer disabled:opacity-60 disabled:cursor-not-allowed focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-1 ${VARIANTS[variant]} ${className}`}
    >
      {loading ? <Loader2 size={16} className="animate-spin" /> : Icon ? <Icon size={16} /> : null}
      {children}
    </button>
  );
}

export function PageHeader({
  title,
  subtitle,
  actions,
}: {
  title: string;
  subtitle?: string;
  actions?: React.ReactNode;
}) {
  return (
    <header className="bg-white border-b border-slate-200 px-8 py-5">
      <div className="max-w-6xl mx-auto flex items-center justify-between gap-4">
        <div>
          <h1 className="text-xl font-bold text-slate-900 tracking-tight">{title}</h1>
          {subtitle && <p className="text-sm text-slate-500 mt-0.5">{subtitle}</p>}
        </div>
        {actions && <div className="flex items-center gap-2 shrink-0">{actions}</div>}
      </div>
    </header>
  );
}

export function Card({
  children,
  className = "",
  onClick,
}: {
  children: React.ReactNode;
  className?: string;
  onClick?: () => void;
}) {
  return (
    <div
      onClick={onClick}
      className={`bg-white rounded-xl border border-slate-200 shadow-sm ${
        onClick ? "cursor-pointer hover:border-brand-300 hover:shadow transition-all duration-150" : ""
      } ${className}`}
    >
      {children}
    </div>
  );
}

export function EmptyState({
  icon: Icon,
  title,
  hint,
  action,
}: {
  icon: React.ComponentType<{ size?: number | string; className?: string }>;
  title: string;
  hint?: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center text-center py-16 px-6">
      <div className="w-14 h-14 rounded-2xl bg-brand-50 flex items-center justify-center mb-4">
        <Icon size={26} className="text-brand-600" />
      </div>
      <h3 className="text-base font-semibold text-slate-800">{title}</h3>
      {hint && <p className="text-sm text-slate-500 mt-1 max-w-sm">{hint}</p>}
      {action && <div className="mt-5">{action}</div>}
    </div>
  );
}

export function Badge({
  children,
  tone = "slate",
}: {
  children: React.ReactNode;
  tone?: "slate" | "blue" | "green" | "amber" | "rose";
}) {
  const tones: Record<string, string> = {
    slate: "bg-slate-100 text-slate-600",
    blue: "bg-brand-50 text-brand-700",
    green: "bg-emerald-50 text-emerald-700",
    amber: "bg-amber-50 text-amber-700",
    rose: "bg-rose-50 text-rose-700",
  };
  return (
    <span className={`inline-flex items-center gap-1 text-xs font-medium px-2 py-0.5 rounded-full ${tones[tone]}`}>
      {children}
    </span>
  );
}
