import { useCurrentFrame, interpolate } from "remotion";
import {
  Aperture,
  Base,
  C,
  chinese,
  clamp,
  display,
  Enter,
  Eyebrow,
  Mark,
  mono,
  progress,
} from "../visual";

export const Intro = () => {
  const f = useCurrentFrame();
  return (
    <Base section="CLEAR SIGNAL" number="01">
      <div
        style={{
          position: "absolute",
          left: 55,
          top: 765,
          fontFamily: display,
          fontSize: 320,
          fontWeight: 800,
          letterSpacing: -7,
          color: C.text,
          opacity: 0.022,
          translate: `${-f * 0.7}px 0`,
        }}
      >
        INTELLIGENCE IN MOTION
      </div>
      <div
        style={{
          scale: interpolate(f, [0, 75], [0.65, 1], clamp),
          transformOrigin: "1450px 525px",
          opacity: progress(f, 4, 30),
        }}
      >
        <Aperture x={1465} y={525} frame={f} radius={255} />
      </div>
      <Enter
        at={14}
        y={0}
        style={{
          position: "absolute",
          left: 1385,
          top: 445,
          scale: 0.9 + 0.1 * progress(f, 14, 35),
        }}
      >
        <Mark size={160} />
      </Enter>
      <Enter
        at={7}
        y={-24}
        style={{ position: "absolute", left: 108, top: 314 }}
      >
        <Eyebrow>FROM EXECUTION TO INSIGHT</Eyebrow>
      </Enter>
      <div
        style={{
          position: "absolute",
          left: 93,
          top: 354,
          clipPath: `inset(0 ${100 * (1 - progress(f, 8, 40))}% 0 0)`,
          fontSize: 275,
          fontWeight: 700,
          letterSpacing: -6,
          lineHeight: 1,
        }}
      >
        Code<span style={{ color: C.green }}>Sage</span>
        <span
          style={{
            fontSize: 46,
            verticalAlign: "top",
            display: "inline-block",
            marginTop: 52,
            marginLeft: 18,
          }}
        >
          ↗
        </span>
      </div>
      <Enter
        at={25}
        y={30}
        style={{ position: "absolute", left: 108, top: 670 }}
      >
        <div style={{ fontFamily: chinese, fontSize: 40, letterSpacing: 4 }}>
          可靠执行，深度审计。
        </div>
        <div
          style={{
            fontFamily: mono,
            fontSize: 20,
            marginTop: 24,
            color: C.muted,
            letterSpacing: 4,
          }}
        >
          LESS NOISE. MORE SIGNAL.
        </div>
      </Enter>
      <Enter
        at={32}
        x={30}
        y={0}
        style={{
          position: "absolute",
          left: 1330,
          top: 832,
          fontFamily: mono,
          fontSize: 18,
          color: C.muted,
        }}
      >
        15 SECONDS / INSIDE THE SYSTEM
      </Enter>
    </Base>
  );
};
