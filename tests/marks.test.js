// Extract the real appendTok/contentEl out of index.html and exercise them
// against a minimal DOM shim: every match must be wrapped, nothing else, and
// the visible text must survive untouched.
const fs = require("fs"), assert = require("assert");
const src = fs.readFileSync("seendiff/static/index.html", "utf8");
const grab = name => {
  const i = src.indexOf("function " + name + "(");
  let d = 0, j = src.indexOf("{", i);
  for (let k = j; k < src.length; k++) {
    if (src[k] === "{") d++;
    else if (src[k] === "}" && --d === 0) return src.slice(i, k + 1);
  }
};

class El {
  constructor(tag) { this.tag = tag; this.className = ""; this.kids = []; }
  set textContent(t) { this.kids = [t]; }
  append(...xs) { this.kids.push(...xs); }
  text() { return this.kids.map(k => typeof k === "string" ? k : k.text()).join(""); }
  // [start,end) ranges covered by <mark>, plus the class each piece carries
  marks(pos = 0, out = []) {
    for (const k of this.kids) {
      if (typeof k === "string") { pos += k.length; continue; }
      const len = k.text().length;
      if (k.tag === "mark") out.push([pos, pos + len, k.className]);
      else k.marks(pos, out);
      pos += len;
    }
    return out;
  }
  classes(pos = 0, out = []) {
    for (const k of this.kids) {
      if (typeof k === "string") { pos += k.length; continue; }
      if (k.tag === "span") out.push([pos, pos + k.text().length, k.className]);
      else k.classes(pos, out);
      pos += k.text().length;
    }
    return out;
  }
}
global.document = { createElement: t => new El(t) };
let sByLine = new Map(), sMatches = [], sIdx = -1;
eval(grab("appendTok"));
eval(grab("contentEl"));

const merge = rs => {           // <mark> runs may be split by token boundaries
  const out = [];
  for (const [s, e] of rs.sort((a, b) => a[0] - b[0])) {
    if (out.length && out[out.length - 1][1] === s) out[out.length - 1][1] = e;
    else out.push([s, e]);
  }
  return out;
};

function check(name, toks, hits) {
  const plain = toks.map(t => t[1]).join("");
  sMatches = hits; sIdx = 0;
  sByLine = new Map([[7, hits]]);
  const el = contentEl({ t: toks }, 7);
  assert.strictEqual(el.text(), plain, name + ": text changed");
  // marked characters are exactly the matched ones (a mark may be split at a
  // token boundary, and two adjacent matches stay two marks — so compare the
  // merged unions), and no mark may straddle two different matches
  const raw = el.marks();
  assert.deepStrictEqual(merge(raw), merge(hits.map(h => [h.s, h.e])),
                         name + ": wrong mark ranges");
  for (const [s, e] of raw)
    assert.ok(hits.some(h => h.s <= s && e <= h.e),
              name + `: mark ${s}-${e} straddles matches`);
  // every character keeps the syntax class its token gave it
  const want = [];
  let p = 0;
  // an empty token contributes no span at all, which is what we want
  for (const [cls, t] of toks) { if (cls && t) want.push([p, p + t.length, cls]); p += t.length; }
  assert.deepStrictEqual(merge3(el.classes()), merge3(want), name + ": class lost");
  console.log("ok  " + name);
}
function merge3(rs) {
  const out = [];
  for (const [s, e, c] of rs.sort((a, b) => a[0] - b[0])) {
    const last = out[out.length - 1];
    if (last && last[1] === s && last[2] === c) last[1] = e;
    else out.push([s, e, c]);
  }
  return out;
}

const T = [["k", "def"], ["", " "], ["nf", "alpha_beta"], ["p", "():"]];
check("match inside one token", T, [{ i: 7, s: 4, e: 9 }]);
check("match straddling three tokens", T, [{ i: 7, s: 1, e: 12 }]);
check("two matches in one token", T, [{ i: 7, s: 4, e: 6 }, { i: 7, s: 7, e: 9 }]);
check("match at line start", T, [{ i: 7, s: 0, e: 3 }]);
check("match at line end", T, [{ i: 7, s: 14, e: 17 }]);
check("whole line", T, [{ i: 7, s: 0, e: 17 }]);
check("adjacent matches", T, [{ i: 7, s: 0, e: 4 }, { i: 7, s: 4, e: 8 }]);
check("untokenized line", [["", "plain text here"]], [{ i: 7, s: 6, e: 10 }]);
check("empty token in the middle",
      [["k", "def"], ["x", ""], ["nf", "f"]], [{ i: 7, s: 2, e: 4 }]);

// no hits at all: the untouched fast path
sByLine = new Map(); sMatches = []; sIdx = -1;
assert.strictEqual(contentEl({ t: T }, 7).text(), "def alpha_beta():");
assert.strictEqual(contentEl({ t: T }, 7).marks().length, 0);
assert.strictEqual(contentEl({ text: "raw" }, null).text(), "raw");
console.log("ok  no-hit fast path");

// the current match gets the .cur class, the others don't
const hits = [{ i: 7, s: 0, e: 3 }, { i: 7, s: 4, e: 9 }];
sMatches = hits; sIdx = 1; sByLine = new Map([[7, hits]]);
assert.deepStrictEqual(contentEl({ t: T }, 7).marks().map(m => m[2]), ["", "cur"]);
console.log("ok  current-match class");
console.log("\nall mark tests passed");
