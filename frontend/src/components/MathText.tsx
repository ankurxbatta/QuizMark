"use client";
import React from "react";
import katex from "katex";

/**
 * Renders a string that mixes prose with LaTeX math. Math wrapped in $...$,
 * $$...$$, \(...\) or \[...\] is rendered with KaTeX; everything else is plain
 * text with newlines preserved as <br/>. Falls back to the raw expression if a
 * fragment fails to parse, so it never throws on imperfect generated LaTeX.
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

// Matches $$...$$, \[...\], \(...\) and $...$ (single-line) math spans.
const TOKEN = /(\$\$[\s\S]+?\$\$|\\\[[\s\S]+?\\\]|\\\([\s\S]+?\\\)|\$[^$\n]+?\$)/g;

function textToNodes(s: string, keyBase: string): React.ReactNode[] {
  const out: React.ReactNode[] = [];
  s.split("\n").forEach((line, idx) => {
    if (idx > 0) out.push(<br key={`${keyBase}-br${idx}`} />);
    if (line) out.push(<React.Fragment key={`${keyBase}-l${idx}`}>{line}</React.Fragment>);
  });
  return out;
}

export default function MathText({
  text,
  className,
}: {
  text?: string | null;
  className?: string;
}) {
  if (!text) return null;
  if (!text.includes("$") && !text.includes("\\(") && !text.includes("\\[")) {
    // Fast path: no math at all — still preserve newlines.
    return <span className={className}>{textToNodes(text, "p")}</span>;
  }

  const parts: React.ReactNode[] = [];
  let last = 0;
  let i = 0;
  let m: RegExpExecArray | null;
  TOKEN.lastIndex = 0;
  while ((m = TOKEN.exec(text)) !== null) {
    if (m.index > last) parts.push(...textToNodes(text.slice(last, m.index), `t${i}`));
    const raw = m[0];
    let display = false;
    let inner = raw;
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
    parts.push(
      <span key={`m${i}`} dangerouslySetInnerHTML={{ __html: renderMath(inner.trim(), display) }} />
    );
    last = m.index + raw.length;
    i++;
  }
  if (last < text.length) parts.push(...textToNodes(text.slice(last), `t${i}`));

  return <span className={className}>{parts}</span>;
}
