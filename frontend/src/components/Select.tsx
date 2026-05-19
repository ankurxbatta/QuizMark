"use client";
import { useState, useRef, useEffect, useId } from "react";
import { ChevronDown, Check } from "lucide-react";

export interface SelectOption {
  value: string;
  label: string;
}

interface SelectProps {
  value: string;
  onChange: (value: string) => void;
  options: SelectOption[];
  disabled?: boolean;
  className?: string;
  /** Label shown above the trigger (optional – mirrors the existing pattern) */
  label?: string;
  labelClassName?: string;
}

export default function Select({
  value,
  onChange,
  options,
  disabled = false,
  className = "",
  label,
  labelClassName,
}: SelectProps) {
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const id = useId();

  const selected = options.find((o) => o.value === value);

  // Close on outside click / Escape
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    const onClick = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", onClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  // Keyboard navigation inside the open list
  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (disabled) return;
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      setOpen((o) => !o);
    }
    if (!open) return;
    const idx = options.findIndex((o) => o.value === value);
    if (e.key === "ArrowDown") {
      e.preventDefault();
      const next = options[(idx + 1) % options.length];
      onChange(next.value);
    }
    if (e.key === "ArrowUp") {
      e.preventDefault();
      const prev = options[(idx - 1 + options.length) % options.length];
      onChange(prev.value);
    }
  };

  return (
    <div ref={containerRef} className={`relative w-full ${className}`}>
      {label && (
        <label
          htmlFor={id}
          className={labelClassName ?? "text-xs font-medium text-gray-500 uppercase block mb-1"}
        >
          {label}
        </label>
      )}

      {/* Trigger button */}
      <button
        id={id}
        type="button"
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => !disabled && setOpen((o) => !o)}
        onKeyDown={handleKeyDown}
        style={{ appearance: "none", WebkitAppearance: "none" }}
        className={[
          "w-full flex items-center justify-between gap-2",
          "mt-1 border rounded-lg px-3 py-2 text-sm text-left",
          "bg-white text-gray-800 font-sans",
          "transition-colors cursor-pointer outline-none",
          "shadow-none",
          open
            ? "border-indigo-500 ring-2 ring-indigo-500 ring-offset-0"
            : "border-gray-300 hover:border-indigo-400",
          disabled ? "opacity-60 cursor-not-allowed" : "",
        ]
          .filter(Boolean)
          .join(" ")}
      >
        <span className="truncate">{selected?.label ?? "Select…"}</span>
        <ChevronDown
          size={15}
          className={`shrink-0 text-indigo-500 transition-transform duration-150 ${
            open ? "rotate-180" : ""
          }`}
        />
      </button>

      {/* Dropdown panel */}
      {open && (
        <ul
          role="listbox"
          className="absolute z-50 mt-1 w-full bg-white border border-indigo-200 rounded-xl shadow-lg py-1 overflow-auto max-h-60"
        >
          {options.map((opt) => {
            const isSelected = opt.value === value;
            return (
              <li
                key={opt.value}
                role="option"
                aria-selected={isSelected}
                onClick={() => {
                  onChange(opt.value);
                  setOpen(false);
                }}
                className={[
                  "flex items-center justify-between gap-2",
                  "px-3 py-2 text-sm cursor-pointer select-none",
                  "transition-colors",
                  isSelected
                    ? "bg-indigo-50 text-indigo-700 font-medium"
                    : "text-gray-700 hover:bg-indigo-50 hover:text-indigo-700",
                ].join(" ")}
              >
                <span className="truncate">{opt.label}</span>
                {isSelected && <Check size={14} className="shrink-0 text-indigo-500" />}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
