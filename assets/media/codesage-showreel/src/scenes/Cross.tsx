import { useCurrentFrame } from "remotion";
import {
  Aperture,
  Base,
  C,
  chinese,
  Enter,
  Eyebrow,
  Headline,
  mono,
  Panel,
  progress,
  Wire,
} from "../visual";

export const Cross = () => {
  const f = useCurrentFrame();
  const checks = [
    "证据验证",
    "对抗挑战",
    "一致性验证",
    "跨文件复合风险",
    "重复关系判断",
  ];
  return (
    <Base section="CROSS ANALYSIS" number="04" independent>
      <Enter at={5} style={{ position: "absolute", top: 199, left: 100 }}>
        <Eyebrow>ONE HARNESS. CONNECTED REASONING.</Eyebrow>
        <Headline size={148}>
          CROSS. VERIFY. <span style={{ color: C.green }}>CONNECT.</span>
        </Headline>
      </Enter>
      <Aperture
        x={957}
        y={645}
        frame={f}
        radius={203}
        opacity={progress(f, 15, 28)}
      />
      <svg width="1920" height="1080" style={{ position: "absolute" }}>
        {[478, 615, 752].map((y, i) => (
          <Wire
            key={y}
            d={`M522 ${y} C635 ${y} 620 645 747 645`}
            at={22 + i * 5}
          />
        ))}
        <Wire d="M1167 645 L1320 645" at={70} />
      </svg>
      {["CANDIDATE INDEX", "EXTRACTED EVIDENCE", "CODE ON DEMAND"].map(
        (label, i) => (
          <Enter
            key={label}
            at={15 + i * 6}
            x={-35}
            y={0}
            style={{ position: "absolute", left: 100, top: 426 + i * 137 }}
          >
            <Panel style={{ width: 422, height: 105, padding: "20px 25px" }}>
              <div
                style={{
                  fontFamily: mono,
                  fontSize: 21,
                  color: i === 1 ? C.green : C.text,
                }}
              >
                {label}
              </div>
              <div
                style={{
                  fontFamily: chinese,
                  fontSize: 23,
                  color: C.muted,
                  marginTop: 5,
                }}
              >
                {
                  [
                    "候选是待验证主张",
                    "确定性提取真实 diff 证据",
                    "只读工具读取固定快照",
                  ][i]
                }
              </div>
            </Panel>
          </Enter>
        ),
      )}
      <Enter
        at={35}
        y={0}
        style={{
          position: "absolute",
          left: 805,
          top: 572,
          width: 304,
          textAlign: "center",
        }}
      >
        <div
          style={{
            fontSize: 87,
            fontWeight: 700,
            lineHeight: 1,
            color: C.green,
          }}
        >
          CROSS
        </div>
        <div style={{ fontFamily: mono, fontSize: 21, marginTop: 15 }}>
          VERIFY → CHALLENGE
        </div>
      </Enter>
      <Enter
        at={50}
        x={35}
        y={0}
        style={{ position: "absolute", left: 1320, top: 421 }}
      >
        <Panel style={{ width: 500, height: 418, padding: 28 }}>
          <div style={{ fontSize: 40, fontWeight: 600, marginBottom: 19 }}>
            EVIDENCE BEFORE VERDICT
          </div>
          {checks.map((label, i) => (
            <div
              key={label}
              style={{
                height: 53,
                display: "flex",
                alignItems: "center",
                gap: 18,
                borderTop: `1px solid ${C.line}`,
              }}
            >
              <span
                style={{
                  fontFamily: mono,
                  color: C.green,
                  fontSize: 25,
                  opacity: progress(f, 65 + i * 13, 18),
                }}
              >
                ✓
              </span>
              <span style={{ fontFamily: chinese, fontSize: 28 }}>{label}</span>
            </div>
          ))}
        </Panel>
      </Enter>
      <Enter
        at={95}
        style={{
          position: "absolute",
          left: 100,
          top: 905,
          display: "flex",
          alignItems: "center",
          gap: 24,
        }}
      >
        <span style={{ fontFamily: mono, fontSize: 24, color: C.green }}>
          FinalizeReview
        </span>
        <span style={{ color: C.muted, fontSize: 28 }}>→</span>
        <span style={{ fontFamily: chinese, fontSize: 27 }}>
          确定性去重 / 评分 / 合并门禁
        </span>
        <span style={{ color: C.green, fontSize: 28 }}>→</span>
        <span style={{ fontFamily: chinese, fontSize: 27 }}>
          最终报告 + 过程报告
        </span>
      </Enter>
    </Base>
  );
};
