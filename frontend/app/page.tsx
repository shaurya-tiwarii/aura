const capabilities = [
  ["Website analysis", "Crawl, SEO scoring, and estimated AI-search readiness"],
  ["Actionable recommendations", "Prioritized fixes with explainable impact"],
  ["Content assistance", "Structured Gemini explanations and rewrites"]
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
          <p className="eyebrow">SEARCH VISIBILITY INTELLIGENCE</p>
          <h1>Make your business easier to find and understand.</h1>
          <p className="lede">
            AURA turns your website into a clear action plan using transparent
            analysis, honest metrics, and guarded AI assistance.
          </p>
          <div className="actions">
            <a className="button" href="/login">Open your workspace</a>
            <a className="text-link" href="#capabilities">See capabilities →</a>
          </div>
        </div>
        <HeroScene />
      </section>
      <section id="capabilities" className="cards">
        {capabilities.map(([title, description]) => (
          <article className="card" key={title}>
            <h2>{title}</h2>
            <p>{description}</p>
          </article>
        ))}
      </section>
      <p className="disclaimer">
        Scores are explainable estimates, not guarantees of search ranking or AI citation.
      </p>
    </main>
  );
}
import HeroScene from "./components/HeroScene";
