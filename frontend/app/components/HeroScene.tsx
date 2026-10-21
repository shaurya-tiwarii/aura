"use client";

import { useState } from "react";

export default function HeroScene() {
  const [tilt, setTilt] = useState({ x: 0, y: 0 });

  function move(event: React.PointerEvent<HTMLDivElement>) {
    const bounds = event.currentTarget.getBoundingClientRect();
    const x = ((event.clientX - bounds.left) / bounds.width - 0.5) * 2;
    const y = ((event.clientY - bounds.top) / bounds.height - 0.5) * 2;
    setTilt({ x: y * -8, y: x * 10 });
  }

  return (
    <div
      className="scene"
      onPointerMove={move}
      onPointerLeave={() => setTilt({ x: 0, y: 0 })}
      aria-label="Decorative illustration of search signals. No real data is shown."
      role="img"
      style={{ "--tilt-x": `${tilt.x}deg`, "--tilt-y": `${tilt.y}deg` } as React.CSSProperties}
    >
      <div className="scene-grid" />
      <div className="scene-halo" />
      <div className="scene-orbit orbit-one"><span className="signal signal-a" /></div>
      <div className="scene-orbit orbit-two"><span className="signal signal-b" /></div>
      <div className="scene-core">
        <div className="core-shine" />
        <div className="core-ring ring-one" />
        <div className="core-ring ring-two" />
        <span className="core-dot" />
      </div>
      <div className="scene-card card-top"><span className="pulse-dot" /> Signal field <strong>illustration</strong></div>
      <div className="scene-card card-bottom"><small>DECORATIVE ARTWORK</small><strong>No real data shown</strong></div>
      <div className="scene-caption">AURA / SIGNAL FIELD · ILLUSTRATIVE ONLY</div>
    </div>
  );
}
