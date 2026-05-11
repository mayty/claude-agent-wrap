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

// 1. Code blocks (triple backticks) — capture optional language tag for Telegram highlighting
t = t.replace(/```([a-zA-Z0-9_+.#-]+)?\n?([\s\S]*?)```/g, (_, lang, code) => {
    // Remove leading/trailing newlines
    const trimmed = code.replace(/^\n/, '').replace(/\n$/, '').trim();
    const p = placeholder(blocks.length);
    if (!trimmed) {
        // Skip empty code blocks — Telegram ignores empty <pre> tags
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

// 2. Bold — before inline code so placeholders don't get trapped
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

// 3. Italic — star syntax only (underscore is underline in Telegram)
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

// 4. Inline code — handles remaining inline code not already captured by bold/italic
t = t.replace(/`([^`]+)`/g, (_, code) => {
    const esc = code.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    const p = placeholder(blocks.length);
    blocks.push('<code>' + esc + '</code>');
    return p;
});

// 5. Links
t = t.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_, label, url) => {
    const esc = label.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    const escUrl = url.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    const p = placeholder(blocks.length);
    blocks.push(`<a href="${escUrl}">${esc}</a>`);
    return p;
});

// 6. Strikethrough
t = t.replace(/~~(.+?)~~/g, (_, inner) => {
    const p = placeholder(blocks.length);
    blocks.push('<s>' + inner + '</s>');
    return p;
});

// 7. Escape remaining text
t = t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

// 8. Restore protected blocks (reverse order to handle nesting)
for (let i = blocks.length - 1; i >= 0; i--) {
    t = t.replace(placeholder(i), blocks[i]);
}

console.log(t);
