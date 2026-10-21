import HeroScene from "./components/HeroScene";
import Reveal from "./components/Reveal";

const chapters = [
  {
    num: "01",
    eyebrow: "Crawl and score",
    title: <>Analyze: two scores, every point <span className="serif-accent">accounted for</span></>,
    body: "AURA fetches up to 20 pages of your site, reads the HTML the way a crawler does, and scores it twice. The SEO engine checks 27 rules across 8 categories. The AI-readiness engine checks 23 signals across 8 categories. Both engines are deterministic arithmetic: run the same scan twice and you get the same scores.",
    specs: [
      ["27 SEO rules", "Metadata, content, technical health, structure, links, schema, images, readability."],
      ["23 AI-readiness signals", "Answerability, schema, structure, entity clarity, completeness, trust, citation readiness, freshness."],
      ["Stored per scan", "Scans are immutable. Each one keeps its scores, its issues, and the pages behind them."]
    ]
  },
  {
    num: "02",
    eyebrow: "The fix list",
    title: <>Optimize: a fix list ranked by <span className="serif-accent">what it recovers</span></>,
    body: "Findings become recommendations, ranked by estimated score gain divided by effort. A missing meta description that costs six points outranks a rewrite that costs one. Each recommendation names the rule or signal behind it, the pages it affects, and the next step to take.",
    specs: [
      ["Impact from arithmetic", "Estimated gain is the points the fix would recover, computed from the score itself, not a guess."],
      ["Effort is a fixed table", "Trivial, low, medium, high. The biggest gain for the least work sorts first."],
      ["Your calls stay yours", "Dismiss a recommendation or mark it done. A later scan does not override your manual states."]
    ]
  },
  {
    num: "03",
    eyebrow: "Write, approve, post",
    title: <>Publish: drafts you <span className="serif-accent">approve</span> before anything posts</>,
    body: "AURA drafts posts, FAQs, and page copy from your business profile, then scores each draft. You read it, edit it, and mark it ready. Only then can it be published or scheduled. Nothing goes out on its own.",
    specs: [
      ["Gemini, in its place", "Generation, rewrites, and explanations only. Scoring and crawling never use the model."],
      ["Demo connectors, labelled", "WordPress, LinkedIn, and Reddit connectors run in clearly labelled demo mode until you add real platform credentials."],
      ["Scheduled, with retries", "Posts can be queued for a set time. A failed publish retries and stays visible instead of getting lost."]
    ]
  },
  {
    num: "04",
    eyebrow: "Proof over time",
    title: <>Grow: improvement you can <span className="serif-accent">point to</span></>,
    body: "Set a monitoring schedule and AURA rescans on its own. The improvement view diffs any two scans: score deltas per category, issues resolved, issues introduced. A recommendation is credited only when the specific issue behind it is gone in a later scan.",
    specs: [
      ["Honest deltas", "Scores compare only when the engine version matches. Across versions, AURA says so instead of printing a misleading delta."],
      ["Analytics with provenance", "Every number is labelled: platform API, demo, entered by you, or derived. A demo number is always visibly a demo number."]
    ]
  }
];

export default function Home() {
  return (
    <main className="shell">
      <nav className="nav">
        <strong className="brand">AURA</strong>
        <span className="tagline">Analyze. Optimize. Publish. Grow.</span>
        <a className="button secondary" href="/login">Sign in</a>
      </nav>

      <section className="hero">
        <div>
          <p className="eyebrow rise d1">Search visibility intelligence</p>
          <h1 className="rise d2">
            Make your business <span className="serif-accent">easier to find</span> and{" "}
            <span className="grad-text">easier to understand</span>.
          </h1>
          <p className="lede rise d3">
            AURA crawls your website and scores it with two deterministic engines:
            an SEO score built from 27 rules, and an estimated AI-search readiness
            score built from 23 signals. Every point lost traces back to a specific
            rule. AI helps explain and write. It never invents the numbers.
          </p>
          <div className="actions rise d4">
            <a className="button" href="/login">Open your workspace</a>
            <a className="text-link" href="#capabilities">See how it works</a>
          </div>
          <p className="hero-meta rise d5">27 SEO RULES · 23 AI-READINESS SIGNALS · ONE FIX LIST</p>
        </div>
        <div className="rise d3">
          <HeroScene />
        </div>
      </section>

      <Reveal>
        <div className="terminal" role="img" aria-label="Sample AURA scan session, shown for illustration only.">
          <div className="term-head">
            <span className="term-dot r" /><span className="term-dot a" /><span className="term-dot g" />
            <span className="term-title">aura · scan session</span>
            <span className="term-badge">SAMPLE</span>
          </div>
          <div className="term-body">
            <div className="term-line"><span className="t-prompt">$</span> aura scan https://brightcrumb-bakery.example</div>
            <div className="term-line"><span className="t-dim">  robots.txt found · sitemap.xml found · crawl cap 20 pages</span></div>
            <div className="term-line"><span className="t-dim">  fetched 18 pages · 2 failed · scored by 2 engines</span></div>
            <div className="term-line">  SEO score       <span className="t-hi">71 / 100</span>  <span className="t-violet">DVO-SEO-SCORE-V2.0 · 27 rules</span></div>
            <div className="term-line">  AI readiness    <span className="t-hi">46 / 100</span>  <span className="t-violet">DVO-AI-VIS-V2.0 · 23 signals · estimate</span></div>
            <div className="term-line">  Top deduction   <span className="t-hi">meta_description</span> <span className="t-dim">missing on 9 pages</span></div>
            <div className="term-line">  Next action     <span className="t-dim">write the descriptions, then rescan to verify</span></div>
          </div>
        </div>
        <p className="term-caption">
          Sample session. The site, scores, and finding above are written for illustration.
          A real workspace shows only your own scan results.
        </p>
      </Reveal>

      <section id="capabilities" className="chapters">
        {chapters.map((chapter, index) => (
          <Reveal key={chapter.num} delay={index * 60}>
            <article className="chapter">
              <div className="chapter-num" aria-hidden="true">{chapter.num}</div>
              <div className="chapter-body">
                <p className="chapter-eyebrow">{chapter.eyebrow}</p>
                <h2>{chapter.title}</h2>
                <p>{chapter.body}</p>
                <ul className="spec-list">
                  {chapter.specs.map(([term, detail]) => (
                    <li key={term}><b>{term}</b><span>{detail}</span></li>
                  ))}
                </ul>
              </div>
            </article>
          </Reveal>
        ))}
      </section>

      <p className="disclaimer">
        Scores are explainable estimates, not guarantees of search ranking or AI citation.
        Estimated AI-search readiness is a heuristic based on on-page signals, not a measured ranking.
      </p>
    </main>
  );
}
