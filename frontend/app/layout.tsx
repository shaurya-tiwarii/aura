import type { Metadata } from "next";
import { GeistSans } from "geist/font/sans";
import { GeistMono } from "geist/font/mono";
import { Averia_Serif_Libre } from "next/font/google";
import "./globals.css";

const accentSerif = Averia_Serif_Libre({
  subsets: ["latin"],
  weight: ["300", "400", "700"],
  style: ["italic"],
  variable: "--font-averia",
  display: "swap"
});

export const metadata: Metadata = {
  title: "AURA | Search visibility intelligence",
  description: "Analyze. Optimize. Publish. Grow."
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" className={`${GeistSans.variable} ${GeistMono.variable} ${accentSerif.variable}`}>
      <body>{children}</body>
    </html>
  );
}
