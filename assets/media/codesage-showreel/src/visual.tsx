import React from "react";
import { AbsoluteFill, Easing, interpolate, useCurrentFrame } from "remotion";

export const C = {
  bg: "#101510",
  panel: "#182019",
  line: "#354431",
  text: "#F2F2E8",
  muted: "#9BA993",
  green: "#D3F86A",
  red: "#ED947D",
};
export const display = '"Barlow Condensed", "Microsoft YaHei", sans-serif';
export const mono =
  '"IBM Plex Mono", "Microsoft YaHei", "Noto Sans CJK SC", monospace';
export const chinese = '"Microsoft YaHei", "Noto Sans CJK SC", sans-serif';
export const clamp = {
  extrapolateLeft: "clamp" as const,
  extrapolateRight: "clamp" as const,
};
export const out = Easing.bezier(0.16, 1, 0.3, 1);
export const progress = (frame: number, start: number, duration: number) =>
  interpolate(frame, [start, start + duration], [0, 1], {
    ...clamp,
    easing: out,
  });

export const Enter: React.FC<
  React.PropsWithChildren<{
    at?: number;
    x?: number;
    y?: number;
    style?: React.CSSProperties;
  }>
> = ({ at = 8, x = 0, y = 25, style, children }) => {
  const f = useCurrentFrame();
  return (
    <div
      style={{
        ...style,
        opacity: progress(f, at, 22),
        translate: `${x * (1 - progress(f, at, 30))}px ${y * (1 - progress(f, at, 30))}px`,
      }}
    >
      {children}
    </div>
  );
};

export const Base: React.FC<
  React.PropsWithChildren<{
    section: string;
    number: string;
    independent?: boolean;
  }>
> = ({ children, section, number, independent }) => {
  const f = useCurrentFrame();
  return (
    <AbsoluteFill
      style={{
        background: C.bg,
        color: C.text,
        fontFamily: display,
        overflow: "hidden",
      }}
    >
      <AbsoluteFill
        style={{
          background:
            "radial-gradient(ellipse at 75% 60%, #253322 0%, #101510 63%)",
          opacity: 0.65,
        }}
      />
      <svg
        width="1920"
        height="1080"
        style={{ position: "absolute", opacity: 0.38 }}
        aria-hidden
      >
        {[100, 960, 1820].map((x) => (
          <line
            key={x}
            x1={x}
            x2={x}
            y1="0"
            y2="1080"
            stroke={C.line}
            strokeDasharray="2 18"
          />
        ))}
        <line x1="100" y1="123" x2="1820" y2="123" stroke={C.line} />
        <line x1="100" y1="974" x2="1820" y2="974" stroke={C.line} />
        {Array.from({ length: 20 }, (_, i) => (
          <circle
            key={i}
            cx={120 + ((i * 167 + f * ((i % 3) + 1) * 0.3) % 1680)}
            cy={160 + ((i * 107) % 780)}
            r={i % 4 === 0 ? 2 : 1}
            fill={C.muted}
            opacity={0.15 + 0.1 * Math.sin(f * 0.025 + i)}
          />
        ))}
      </svg>
      <div
        style={{
          position: "absolute",
          left: 100,
          top: 63,
          display: "flex",
          alignItems: "center",
          gap: 18,
        }}
      >
        <Mark size={35} />
        <span style={{ fontSize: 35, fontWeight: 700, letterSpacing: -0.5 }}>
          CodeSage
        </span>
        <span
          style={{
            fontFamily: mono,
            fontSize: 17,
            color: C.muted,
            marginLeft: 16,
          }}
        >
          {" "}
          / {section}
        </span>
      </div>
      <div
        style={{
          position: "absolute",
          right: 100,
          top: 71,
          fontFamily: mono,
          fontSize: 19,
          color: C.green,
        }}
      >
        {number} / 05
      </div>
      {independent && (
        <div
          style={{
            position: "absolute",
            right: 100,
            top: 148,
            fontSize: 23,
            fontFamily: chinese,
            color: C.green,
            padding: "10px 18px",
            border: `1px solid ${C.line}`,
            background: C.panel,
          }}
        >
          独立运行 · Worker 接入待完成
        </div>
      )}
      {children}
      <div
        style={{
          position: "absolute",
          left: 100,
          bottom: 56,
          fontFamily: mono,
          fontSize: 16,
          color: C.muted,
          letterSpacing: 2,
        }}
      >
        ENGINEERED FOR CLEAR SIGNAL
      </div>
      <div
        style={{
          position: "absolute",
          right: 100,
          bottom: 56,
          fontFamily: mono,
          fontSize: 16,
          color: C.muted,
        }}
      >
        CODESAGE / DEEP REVIEW
      </div>
    </AbsoluteFill>
  );
};

export const Mark = ({
  size = 50,
  color = C.green,
}: {
  size?: number;
  color?: string;
}) => (
  <svg width={size} height={size} viewBox="0 0 64 64">
    <path
      d="M23 10 4 32l19 22M41 10l19 22-19 22M38 17 26 47"
      fill="none"
      stroke={color}
      strokeWidth="6"
      strokeLinejoin="miter"
    />
  </svg>
);

export const Eyebrow = ({ children }: React.PropsWithChildren) => (
  <div
    style={{
      fontFamily: mono,
      color: C.green,
      fontSize: 24,
      letterSpacing: 4,
      marginBottom: 12,
    }}
  >
    {children}
  </div>
);
export const Headline: React.FC<React.PropsWithChildren<{ size?: number }>> = ({
  children,
  size = 150,
}) => (
  <div
    style={{
      fontFamily: display,
      fontWeight: 700,
      fontSize: size,
      letterSpacing: -2.5,
      lineHeight: 0.96,
    }}
  >
    {children}
  </div>
);

export const Wire = ({
  d,
  at = 25,
  color = C.green,
  speed = 95,
}: {
  d: string;
  at?: number;
  color?: string;
  speed?: number;
}) => {
  const f = useCurrentFrame();
  const p = progress(f, at, 36);
  return (
    <g opacity={p}>
      <path d={d} fill="none" stroke={C.line} strokeWidth="2" />
      <path
        d={d}
        fill="none"
        stroke={color}
        strokeWidth="2"
        pathLength="1000"
        strokeDasharray="1000"
        strokeDashoffset={1000 * (1 - p)}
        opacity=".65"
      />
      <path
        d={d}
        fill="none"
        stroke={color}
        strokeWidth="7"
        strokeLinecap="round"
        pathLength="1000"
        strokeDasharray="14 986"
        strokeDashoffset={-(((f - at) / speed) * 1000) % 1000}
        opacity={p}
      />
    </g>
  );
};

export const Panel = ({
  children,
  style,
}: React.PropsWithChildren<{ style?: React.CSSProperties }>) => (
  <div
    style={{
      border: `1px solid ${C.line}`,
      background: C.panel,
      borderRadius: 14,
      ...style,
    }}
  >
    {children}
  </div>
);

export const Aperture = ({
  frame,
  radius = 240,
  x,
  y,
  opacity = 1,
}: {
  frame: number;
  radius?: number;
  x: number;
  y: number;
  opacity?: number;
}) => (
  <svg
    style={{
      position: "absolute",
      left: x - radius - 30,
      top: y - radius - 30,
      opacity,
    }}
    width={radius * 2 + 60}
    height={radius * 2 + 60}
    viewBox={`${-radius - 30} ${-radius - 30} ${radius * 2 + 60} ${radius * 2 + 60}`}
  >
    <circle r={radius} fill="none" stroke={C.line} strokeWidth="1" />
    <g transform={`rotate(${frame * 0.6})`}>
      <circle
        r={radius - 20}
        fill="none"
        stroke={C.green}
        strokeWidth="3"
        strokeDasharray={`${radius * 1.1} ${radius * 0.35} ${radius * 0.3} ${radius * 1.4}`}
      />
      {Array.from({ length: 40 }, (_, i) => (
        <line
          key={i}
          x1={radius + 8}
          x2={radius + (i % 5 === 0 ? 22 : 14)}
          y1="0"
          y2="0"
          transform={`rotate(${i * 9})`}
          stroke={C.muted}
          strokeWidth={i % 5 === 0 ? 2 : 1}
        />
      ))}
    </g>
    <g transform={`rotate(${-frame * 0.35})`}>
      <circle
        r={radius - 56}
        fill="none"
        stroke={C.muted}
        strokeWidth="1"
        strokeDasharray="3 13"
      />
      <path
        d={`M0 ${-radius + 83} A${radius - 83} ${radius - 83} 0 0 1 ${radius - 83} 0`}
        stroke={C.green}
        strokeWidth="12"
        fill="none"
      />
    </g>
  </svg>
);
