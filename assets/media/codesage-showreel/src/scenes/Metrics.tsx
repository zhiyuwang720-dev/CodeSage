import { useCurrentFrame } from "remotion";
import {
  Base,
  C,
  chinese,
  display,
  Enter,
  Eyebrow,
  Headline,
  mono,
  progress,
} from "../visual";
import metrics from "../metrics.json";

export const Metrics = () => {
  const f = useCurrentFrame();
  const p = progress(f, 16, 47);
  return (
    <Base section="BENCHMARK" number="05">
      <Enter at={5} style={{ position: "absolute", top: 181, left: 100 }}>
        <Eyebrow>MEASURED ON AACR-BENCH</Eyebrow>
        <Headline size={150}>
          DEEP REVIEW. <span style={{ color: C.green }}>CLEAR SIGNAL.</span>
        </Headline>
      </Enter>
      <svg
        style={{ position: "absolute", left: 485, top: 355, opacity: 0.2 }}
        width="630"
        height="560"
        viewBox="-315 -280 630 560"
      >
        <circle r="235" fill="none" stroke={C.line} strokeWidth="20" />
        <circle
          r="235"
          fill="none"
          stroke={C.green}
          strokeWidth="20"
          pathLength="100"
          strokeDasharray={`${metrics.precision * p} 100`}
          transform="rotate(-90)"
        />
        <circle
          r="270"
          fill="none"
          stroke={C.line}
          strokeWidth="1"
          strokeDasharray="1 11"
        />
      </svg>
      <Enter
        at={12}
        y={45}
        style={{ position: "absolute", left: 96, top: 409 }}
      >
        <div
          style={{
            fontFamily: mono,
            fontSize: 30,
            color: C.green,
            letterSpacing: 5,
          }}
        >
          PRECISION / 精确率
        </div>
        <div
          style={{
            fontFamily: display,
            fontSize: 303,
            color: C.green,
            fontWeight: 700,
            letterSpacing: -8,
            lineHeight: 1.08,
            fontVariantNumeric: "tabular-nums",
          }}
        >
          {(metrics.precision * p).toFixed(1)}
          <span style={{ fontSize: 122, letterSpacing: -2 }}>%</span>
        </div>
        <div
          style={{
            fontFamily: chinese,
            fontSize: 28,
            color: C.muted,
            marginTop: 5,
          }}
        >
          从并行调查，到有证据的判断。
        </div>
      </Enter>
      <div
        style={{
          position: "absolute",
          top: 421,
          left: 1140,
          width: 1,
          height: 405,
          background: C.line,
        }}
      />
      <Enter
        at={20}
        x={45}
        y={0}
        style={{ position: "absolute", top: 415, left: 1220 }}
      >
        <div
          style={{
            fontFamily: mono,
            fontSize: 24,
            color: C.muted,
            letterSpacing: 3,
          }}
        >
          RECALL / 召回率
        </div>
        <div
          style={{
            fontSize: 155,
            fontWeight: 600,
            lineHeight: 1.13,
            fontVariantNumeric: "tabular-nums",
            letterSpacing: -3,
          }}
        >
          {(metrics.recall * p).toFixed(1)}
          <span style={{ fontSize: 68 }}>%</span>
        </div>
      </Enter>
      <Enter
        at={27}
        x={45}
        y={0}
        style={{ position: "absolute", top: 639, left: 1220 }}
      >
        <div
          style={{
            fontFamily: mono,
            fontSize: 24,
            color: C.muted,
            letterSpacing: 3,
          }}
        >
          F1 SCORE
        </div>
        <div
          style={{
            fontSize: 155,
            fontWeight: 600,
            lineHeight: 1.13,
            fontVariantNumeric: "tabular-nums",
            letterSpacing: -3,
          }}
        >
          {(metrics.f1 * p).toFixed(1)}
          <span style={{ fontSize: 68 }}>%</span>
        </div>
      </Enter>
      <Enter
        at={45}
        y={0}
        style={{
          position: "absolute",
          left: 100,
          top: 902,
          fontFamily: mono,
          fontSize: 21,
          color: C.muted,
          letterSpacing: 1,
        }}
      >
        GLM-5.2 <span style={{ color: C.line }}> / </span> SEMANTIC METRICS{" "}
        <span style={{ color: C.line }}> / </span> 2026.09.28{" "}
        <span style={{ color: C.line }}> / </span> RUN: deep-GLM-5.2
      </Enter>
    </Base>
  );
};
