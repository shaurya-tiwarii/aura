"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { supabase } from "../../../lib/supabase";

export default function AuthCallbackPage() {
  const router = useRouter();
  const [message, setMessage] = useState("Finishing secure sign-in…");

  useEffect(() => {
    if (!supabase) {
      setMessage("Supabase sign-in is not configured for this workspace.");
      return;
    }
    supabase.auth.getSession().then(({ data, error }) => {
      if (error || !data.session) {
        setMessage(error?.message ?? "No sign-in session was returned.");
        return;
      }
      localStorage.setItem("aura_access_token", data.session.access_token);
      localStorage.setItem("aura_auth_provider", "supabase");
      localStorage.setItem("aura_user", JSON.stringify(data.session.user));
      router.replace("/dashboard");
    });
  }, [router]);

  return <main className="loading"><div><p className="eyebrow">SECURE AUTHENTICATION</p><h1>{message}</h1></div></main>;
}
