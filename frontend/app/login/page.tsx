"use client";

import { FormEvent, useState } from "react";
import { api, saveSession } from "../../lib/api";
import { supabase, supabaseConfigured } from "../../lib/supabase";
import { useRouter } from "next/navigation";

export default function LoginPage() {
  const router = useRouter();
  const [register, setRegister] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError("");
    try {
      const session = await api<{ tokens: { access_token: string; refresh_token?: string | null }; user: unknown }>(
        register ? "/auth/register" : "/auth/login",
        { method: "POST", body: JSON.stringify(register ? { email, password, full_name: name || null } : { email, password }) },
      );
      saveSession(session, "local"); router.push("/dashboard");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Unable to sign in");
    }
    finally { setBusy(false); }
  }

  async function signInWithGoogle() {
    setError("");
    if (!supabase) {
      setError("Google sign-in is not configured. Add the Supabase browser variables to frontend/.env.local.");
      return;
    }
    const { error: oauthError } = await supabase.auth.signInWithOAuth({
      provider: "google",
      options: { redirectTo: `${window.location.origin}/auth/callback` },
    });
    if (oauthError) setError(oauthError.message);
  }

  return <main className="auth-page"><div className="auth-card">
    <a className="brand" href="/">AURA</a>
    <p className="eyebrow">YOUR SEARCH VISIBILITY WORKSPACE</p>
    <h1>{register ? "Create your workspace" : "Welcome back"}</h1>
    <p className="muted">{register ? "Start with a business and a website. You can add the rest as you go." : "Sign in to analyze, optimize, publish, and grow."}</p>
    {!register && <button type="button" className="button google-button" onClick={signInWithGoogle} disabled={busy}>
      <span aria-hidden="true">G</span> Continue with Google
    </button>}
    {!register && <div className="auth-divider"><span>or continue with email</span></div>}
    <form onSubmit={submit}>
      {register && <label>Full name<input value={name} onChange={e => setName(e.target.value)} autoComplete="name" /></label>}
      <label>Email<input required type="email" value={email} onChange={e => setEmail(e.target.value)} autoComplete="email" /></label>
      <label>Password<input required type="password" value={password} onChange={e => setPassword(e.target.value)} autoComplete={register ? "new-password" : "current-password"} /></label>
      {error && <p className="error">{error}</p>}
      <button className="button wide" disabled={busy}>{busy ? "Working…" : register ? "Create account" : "Sign in"}</button>
    </form>
    <button className="link-button" onClick={() => { setRegister(!register); setError(""); }}>{register ? "Already have an account? Sign in" : "New to AURA? Create an account"}</button>
    <p className="fine-print">Your scores are explainable estimates, not guarantees of ranking or AI citation.</p>
    {!supabaseConfigured && <p className="config-note">Google login becomes available when Supabase is configured.</p>}
  </div></main>;
}
