// This file has been created with the assistance of an AI tool.
// md_to_html.js — Convert markdown to Telegram HTML formatting
// Reads text from first argument, outputs HTML-escaped text with proper tags
'use strict';

const input = process.argv.slice(2).join(' ');
const blocks = [];

function placeholder(idx) {
    return `{{BLOCK${idx}}}`;
}

let t = input;

// 1. Code blocks (triple backticks) — capture optional language tag for Telegram highlighting.
// Fence must open at start-of-line and close on its own line, so inline runs of
// 3+ backticks (e.g. the CommonMark trick for embedding ``` inside prose) aren't
// misread as fences and silently dropped.
t = t.replace(/(^|\n)```([a-zA-Z0-9_+.#-]+)?\n([\s\S]*?)\n```(?=\n|$)/g, (_, pfx, lang, code) => {
    const trimmed = code.replace(/^\n/, '').replace(/\n$/, '').trim();
    const p = pfx + placeholder(blocks.length);
    if (!trimmed) {
        blocks.push('');
        return p;
    }
    const esc = trimmed.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    if (lang) {
        blocks.push(`<pre language="${lang}">${esc}</pre>`);
    } else {
        blocks.push('<pre>' + esc + '</pre>');
    }
    return p;
});

// 1.5. Blockquotes — capture runs of consecutive lines starting with "> ".
// Runs after code fences (so `>` inside a fenced block is already behind a
// placeholder) and before headers/inline passes (so the stripped inner
// content flows through them naturally — e.g. `> **bold**` becomes a quote
// containing bold text). Opening and closing <blockquote> tags are stashed
// as placeholders so they survive the final HTML-escape in pass 8.
t = t.replace(/(^|\n)((?:>[ \t]?[^\n]*(?:\n|$))+)/g, (_, pfx, block) => {
    const inner = block.replace(/^>[ \t]?/gm, '').replace(/\n$/, '');
    const openP = placeholder(blocks.length);
    blocks.push('<blockquote>');
    const rebuilt = pfx + openP + '\n' + inner + '\n';
    const closeP = placeholder(blocks.length);
    blocks.push('</blockquote>');
    return rebuilt + closeP;
});

// Helper: process inline formatting inside bold/italic content
function processInnerFormatting(inner) {
    let result = inner;
    // Process inline code inside bold/italic
    result = result.replace(/`([^`]+)`/g, (_, code) => {
        const esc = code.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        const p = placeholder(blocks.length);
        blocks.push('<code>' + esc + '</code>');
        return p;
    });
    // Process italic inside bold: *text* (underscore is underline in Telegram, handled separately)
    result = result.replace(/(?<!\*)\*([^*]+)\*(?!\*)/g, (_, it) => {
        const p = placeholder(blocks.length);
        blocks.push('<i>' + it + '</i>');
        return p;
    });
    // Process underline inside bold: _text_
    result = result.replace(/(?<!\w)_([^_]+)_(?!\w)/g, (_, it) => {
        const p = placeholder(blocks.length);
        blocks.push('<u>' + it + '</u>');
        return p;
    });
    // Process bold inside italic: **text** or __text__
    result = result.replace(/\*\*(.+?)\*\*/g, (_, b) => {
        const p = placeholder(blocks.length);
        blocks.push('<b>' + b + '</b>');
        return p;
    });
    result = result.replace(/__(.+?)__/g, (_, b) => {
        const p = placeholder(blocks.length);
        blocks.push('<b>' + b + '</b>');
        return p;
    });
    return result;
}

// 2. ATX headers — must run after code-block protection (so fenced blocks
// are already placeholders) and before bold/italic so the heading's inner
// formatting is handled here rather than double-wrapped.
//   H1  → <b><u>…</u></b>
//   H2  → <b><i>…</i></b>
//   H3+ → <i>…</i>
t = t.replace(/^(#{1,6}) +(.+?)[ \t]*#*[ \t]*$/gm, (_, hashes, content) => {
    const inner = processInnerFormatting(content);
    const level = hashes.length;
    let tagged;
    if (level === 1) tagged = '<b><u>' + inner + '</u></b>';
    else if (level === 2) tagged = '<b><i>' + inner + '</i></b>';
    else tagged = '<i>' + inner + '</i>';
    const p = placeholder(blocks.length);
    blocks.push(tagged);
    return p;
});

// 3. Bold — before inline code so placeholders don't get trapped
t = t.replace(/\*\*(.+?)\*\*/g, (_, inner) => {
    const cleaned = processInnerFormatting(inner);
    const p = placeholder(blocks.length);
    blocks.push('<b>' + cleaned + '</b>');
    return p;
});
t = t.replace(/__(.+?)__/g, (_, inner) => {
    const cleaned = processInnerFormatting(inner);
    const p = placeholder(blocks.length);
    blocks.push('<b>' + cleaned + '</b>');
    return p;
});

// 4. Italic — star syntax only (underscore is underline in Telegram)
t = t.replace(/\*(.+?)\*/g, (_, inner) => {
    const cleaned = processInnerFormatting(inner);
    const p = placeholder(blocks.length);
    blocks.push('<i>' + cleaned + '</i>');
    return p;
});
t = t.replace(/(?<!\w)_(.+?)_(?!\w)/g, (_, inner) => {
    const cleaned = processInnerFormatting(inner);
    const p = placeholder(blocks.length);
    blocks.push('<u>' + cleaned + '</u>');
    return p;
});

// 5a. Multi-backtick inline code — match CommonMark: N opening backticks close
// on the next run of exactly N backticks. Handles spans like `` `x` `` or
// ``` `foo` ``` that embed literal backticks. Must run before the single-
// backtick form so the longer fence doesn't get shredded into `…` pairs.
t = t.replace(/(`{2,})([^\n]+?)\1/g, (_, ticks, code) => {
    const trimmed = code.replace(/^ /, '').replace(/ $/, '');
    const esc = trimmed.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    const p = placeholder(blocks.length);
    blocks.push('<code>' + esc + '</code>');
    return p;
});

// 5b. Single-backtick inline code — handles remaining inline code not already captured.
// Exclude newlines from the content class so a lone stray backtick on one line
// can't pair with a backtick many paragraphs later and wrap the intervening
// prose in one giant <code> span.
t = t.replace(/`([^`\n]+)`/g, (_, code) => {
    const esc = code.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    const p = placeholder(blocks.length);
    blocks.push('<code>' + esc + '</code>');
    return p;
});

// 6. Links
t = t.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_, label, url) => {
    const esc = label.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    const escUrl = url.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    const p = placeholder(blocks.length);
    blocks.push(`<a href="${escUrl}">${esc}</a>`);
    return p;
});

// 7. Strikethrough
t = t.replace(/~~(.+?)~~/g, (_, inner) => {
    const p = placeholder(blocks.length);
    blocks.push('<s>' + inner + '</s>');
    return p;
});

// 7.5. Soft-break reflow — collapse single \n inside a paragraph into a
// space so that markdown wrapped at ~80 cols renders as flowing prose in
// Telegram instead of a stack of short lines. Blank lines (\n\n+) keep
// paragraphs apart. Preserve newlines before list-like continuations
// (-, *, +, or "N.") so bulleted/numbered lists don't collapse onto one
// line. Placeholders ({{BLOCKn}}) contain no \n so they're unaffected.
// Hard line breaks ("  \n") are intentionally collapsed to a space too —
// assistant output doesn't emit them.
t = t.split(/\n{2,}/).map(para => {
    return para.replace(/[ \t]*\n[ \t]*(?![-*+]\s|\d+\.\s)/g, ' ');
}).join('\n\n');

// 8. Escape remaining text
t = t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

// 9. Restore protected blocks (reverse order to handle nesting)
for (let i = blocks.length - 1; i >= 0; i--) {
    t = t.replace(placeholder(i), blocks[i]);
}

console.log(t);
