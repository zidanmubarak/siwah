export type SyntaxKind =
  | "text"
  | "keyword"
  | "string"
  | "comment"
  | "number"
  | "function"
  | "type"
  | "punctuation"

export type SyntaxSegment = { text: string; kind: SyntaxKind }

const pythonKeywords = new Set([
  "and",
  "as",
  "assert",
  "async",
  "await",
  "break",
  "class",
  "continue",
  "def",
  "del",
  "elif",
  "else",
  "except",
  "False",
  "finally",
  "for",
  "from",
  "global",
  "if",
  "import",
  "in",
  "is",
  "lambda",
  "None",
  "nonlocal",
  "not",
  "or",
  "pass",
  "raise",
  "return",
  "True",
  "try",
  "while",
  "with",
  "yield",
  "match",
  "case",
])

const jsKeywords = new Set([
  "async",
  "await",
  "break",
  "case",
  "catch",
  "class",
  "const",
  "continue",
  "debugger",
  "default",
  "delete",
  "do",
  "else",
  "export",
  "extends",
  "false",
  "finally",
  "for",
  "from",
  "function",
  "if",
  "implements",
  "import",
  "in",
  "instanceof",
  "interface",
  "let",
  "new",
  "null",
  "of",
  "private",
  "protected",
  "public",
  "return",
  "static",
  "super",
  "switch",
  "this",
  "throw",
  "true",
  "try",
  "type",
  "typeof",
  "undefined",
  "var",
  "void",
  "while",
  "with",
  "yield",
])

const pythonPattern =
  /("""[\s\S]*?"""|'''[\s\S]*?'''|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|#[^\n]*|\b\d+(?:\.\d+)?\b|[A-Za-z_]\w*|\s+|.)/g
const jsPattern =
  /(\/\/[^\n]*|\/\*[\s\S]*?\*\/|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`|\b\d+(?:\.\d+)?\b|[A-Za-z_$]\w*|\s+|.)/g
const genericPattern =
  /("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|#[^\n]*|\b\d+(?:\.\d+)?\b|[A-Za-z_$]\w*|\s+|.)/g

/** Lightweight, deterministic highlighting for tool output and read_file previews. */
export function highlightLine(line: string, filetype: string): SyntaxSegment[] {
  const type = filetype.toLowerCase()
  const python = type === "python" || type === "py"
  const js = [
    "javascript",
    "typescript",
    "typescriptreact",
    "javascriptreact",
    "jsx",
    "tsx",
    "json",
  ].includes(type)
  const pattern = python ? pythonPattern : js ? jsPattern : genericPattern
  const keywords = python ? pythonKeywords : jsKeywords
  const tokens = line.match(pattern) ?? [line]
  const segments: SyntaxSegment[] = []
  let previous = ""
  for (const token of tokens) {
    let kind: SyntaxKind = "text"
    if (token.startsWith("#") || token.startsWith("//") || token.startsWith("/*")) {
      kind = "comment"
    } else if (/^["'`]/.test(token)) {
      kind = "string"
    } else if (/^\d/.test(token)) {
      kind = "number"
    } else if (/^[A-Za-z_$]/.test(token)) {
      if (keywords.has(token)) kind = "keyword"
      else if (previous === "def" || previous === "function") kind = "function"
      else if (previous === "class") kind = "type"
      else if (/^[A-Z]/.test(token)) kind = "type"
    } else if (/^[{}()[\],.;:=+\-*/%<>!&|?]+$/.test(token)) {
      kind = "punctuation"
    }
    segments.push({ text: token, kind })
    if (token.trim().length > 0) previous = token
  }
  return segments
}
