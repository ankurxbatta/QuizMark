"use client";
import React, { useMemo } from "react";
import katex from "katex";

/**
 * Renders an HTML table string (e.g. the backend's `table_html`) while also
 * rendering any inline LaTeX math found in its text cells. The backend escapes
 * cell content with html.escape(), so `<`, `>` and `&` arrive as entities; the
 * `$...$`, `$$...$$`, `\(...\)` and `\[...\]` delimiters survive intact. This
 * component walks the parsed DOM's text nodes and swaps math spans for
 * KaTeX-rendered markup, leaving the surrounding table structure untouched.
 *
 * It is defensive in the same spirit as MathText: it never throws on imperfect
 * LaTeX (falls back to the raw expression), and if anything about the parse
 * fails it falls back to rendering the original HTML verbatim.
 */

function renderMath(tex: string, display: boolean): string {
  try {
    return katex.renderToString(tex, {
      displayMode: display,
      throwOnError: false,
      output: "html",
    });
  } catch {
    return tex.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
}

// Matches $$...$$, \[...\], \(...\) and $...$ (single-line) math spans — same
// delimiter logic as MathText.
const TOKEN = /(\$\$[\s\S]+?\$\$|\\\[[\s\S]+?\\\]|\\\([\s\S]+?\\\)|\$[^$\n]+?\$)/g;

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

/** True when the string contains any recognised math delimiter. */
function hasMath(s: string): boolean {
  return s.includes("$") || s.includes("\\(") || s.includes("\\[");
}

/**
 * Turn a plain-text string (the textContent of a DOM text node) into HTML where
 * math spans are KaTeX markup and everything else is html-escaped literal text.
 */
function textToHtml(text: string): string {
  if (!hasMath(text)) return escapeHtml(text);

  let out = "";
  let last = 0;
  let m: RegExpExecArray | null;
  TOKEN.lastIndex = 0;
  while ((m = TOKEN.exec(text)) !== null) {
    if (m.index > last) out += escapeHtml(text.slice(last, m.index));
    const raw = m[0];
    let display = false;
    let inner: string;
    if (raw.startsWith("$$")) {
      display = true;
      inner = raw.slice(2, -2);
    } else if (raw.startsWith("\\[")) {
      display = true;
      inner = raw.slice(2, -2);
    } else if (raw.startsWith("\\(")) {
      inner = raw.slice(2, -2);
    } else {
      inner = raw.slice(1, -1); // $...$
    }
    out += renderMath(inner.trim(), display);
    last = m.index + raw.length;
  }
  if (last < text.length) out += escapeHtml(text.slice(last));
  return out;
}

/**
 * Walk the DOM, replacing text nodes that contain math with KaTeX markup. Runs
 * only in the browser (DOMParser). Returns the serialized innerHTML, or null if
 * parsing is unavailable / fails, so the caller can fall back to the raw HTML.
 */
function renderMathInHtml(rawHtml: string): string | null {
  if (typeof window === "undefined" || typeof DOMParser === "undefined") {
    return null;
  }
  try {
    // The backend may escape `$` to `&dollar;`/`&#36;`; decode those (only the
    // dollar variants) up front so the delimiter scan can see them. We leave
    // structural `&lt;`/`&gt;`/`&amp;` alone here — DOMParser handles those, and
    // textToHtml re-escapes the per-node textContent it reads back out.
    const prepared = rawHtml
      .replace(/&dollar;/g, "$")
      .replace(/&#36;|&#x24;/gi, "$");

    const doc = new DOMParser().parseFromString(
      `<div id="__twm">${prepared}</div>`,
      "text/html"
    );
    const root = doc.getElementById("__twm");
    if (!root) return null;

    const walker = doc.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const targets: Text[] = [];
    let node: Node | null;
    // eslint-disable-next-line no-cond-assign
    while ((node = walker.nextNode())) {
      const t = node as Text;
      if (t.nodeValue && hasMath(t.nodeValue)) targets.push(t);
    }

    for (const t of targets) {
      const span = doc.createElement("span");
      // nodeValue is already entity-decoded by the parser; render math from it.
      span.innerHTML = textToHtml(t.nodeValue ?? "");
      t.parentNode?.replaceChild(span, t);
    }

    return root.innerHTML;
  } catch {
    return null;
  }
}

export default function TableWithMath({
  html,
  className,
}: {
  /** HTML string for the table (e.g. backend `table_html`). */
  html?: string | null;
  className?: string;
}) {
  const rendered = useMemo(() => {
    if (!html) return "";
    if (!hasMath(html) && !html.includes("&dollar;") && !html.includes("&#36;") && !html.includes("&#x24;")) {
      // No math at all — render the table HTML as-is.
      return html;
    }
    return renderMathInHtml(html) ?? html;
  }, [html]);

  if (!html) return null;

  return <div className={className} dangerouslySetInnerHTML={{ __html: rendered }} />;
}
