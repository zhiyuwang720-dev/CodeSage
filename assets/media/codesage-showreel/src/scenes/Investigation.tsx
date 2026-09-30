import { useCurrentFrame } from "remotion";
import {
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

export const Investigation = () => {
  const f = useCurrentFrame();
  return (
    <Base section="DEEP REVIEW" number="03" independent>
      <Enter at={5} style={{ position: "absolute", top: 199, left: 100 }}>
        <Eyebrow>ASSIGN QUESTIONS. NOT PREDICTED DEFECTS.</Eyebrow>
        <Headline size={145}>
          PLAN <span style={{ color: C.green }}>→</span> PARALLEL REVIEW
        </Headline>
      </Enter>
      <svg width="1920" height="1080" style={{ position: "absolute" }}>
        <Wire d="M307 605 L418 605" at={18} />
        {[480, 650, 820].map((y, i) => (
          <Wire
            key={y}
            d={`M803 605 C930 605 927 ${y} 1080 ${y}`}
            at={32 + i * 7}
          />
        ))}
        {[480, 650, 820].map((y, i) => (
          <Wire
            key={y}
            d={`M1510 ${y} C1620 ${y} 1620 652 1715 652`}
            at={65 + i * 7}
          />
        ))}
      </svg>
      <Enter
        at={13}
        x={-20}
        y={0}
        style={{ position: "absolute", top: 456, left: 100, width: 207 }}
      >
        <div
          style={{
            fontFamily: mono,
            fontSize: 18,
            color: C.muted,
            marginBottom: 17,
          }}
        >
          FIXED SNAPSHOT
        </div>
        {[0, 1, 2].map((i) => (
          <div
            key={i}
            style={{
              background: C.panel,
              border: `1px solid ${C.line}`,
              padding: 15,
              marginTop: 12,
              marginLeft: i * 10,
              height: 72,
            }}
          >
            <div style={{ fontFamily: mono, fontSize: 18, color: C.green }}>
              + DIFF {String(i + 1).padStart(2, "0")}
            </div>
            <div
              style={{
                height: 3,
                width: 80 + i * 16,
                background: C.line,
                marginTop: 13,
              }}
            />
          </div>
        ))}
      </Enter>
      <Enter
        at={20}
        y={35}
        style={{ position: "absolute", top: 470, left: 418 }}
      >
        <Panel
          style={{ width: 385, height: 288, padding: 28, borderColor: C.green }}
        >
          <div style={{ fontFamily: mono, fontSize: 18, color: C.green }}>
            01 / HARNESS
          </div>
          <div style={{ fontSize: 67, fontWeight: 700 }}>PLANNER</div>
          <div style={{ fontFamily: chinese, fontSize: 29, marginTop: 2 }}>
            划分调查工作与覆盖边界
          </div>
          <div
            style={{
              borderTop: `1px solid ${C.line}`,
              marginTop: 23,
              paddingTop: 19,
              fontFamily: chinese,
              fontSize: 23,
              color: C.muted,
            }}
          >
            内聚分组 · 确定性 repair
          </div>
        </Panel>
      </Enter>
      <Enter
        at={25}
        y={0}
        style={{
          position: "absolute",
          top: 804,
          left: 418,
          color: C.muted,
          fontFamily: mono,
          fontSize: 19,
        }}
      >
        SEMANTIC .ai → LEADS, NOT FACTS
      </Enter>
      {["行为与状态", "接口与调用链", "跨文件契约"].map((label, i) => (
        <Enter
          key={label}
          at={37 + i * 8}
          x={30}
          y={0}
          style={{ position: "absolute", left: 1080, top: 414 + i * 170 }}
        >
          <Panel
            style={{
              width: 430,
              height: 133,
              padding: "19px 26px",
              borderColor: i === 0 ? C.green : C.line,
            }}
          >
            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
              }}
            >
              <span style={{ fontFamily: mono, fontSize: 19, color: C.green }}>
                REVIEWER / {String(i + 1).padStart(2, "0")}
              </span>
              <span style={{ fontSize: 19, color: C.muted }}>↗</span>
            </div>
            <div style={{ fontFamily: chinese, fontSize: 31, marginTop: 11 }}>
              {label}
            </div>
            <div
              style={{
                height: 3,
                marginTop: 12,
                width: `${progress(f, 62 + i * 12, 100) * 100}%`,
                background: C.green,
              }}
            />
          </Panel>
        </Enter>
      ))}
      <Enter
        at={85}
        y={0}
        style={{ position: "absolute", left: 1682, top: 600 }}
      >
        <div
          style={{
            width: 105,
            height: 105,
            rotate: "45deg",
            border: `2px solid ${C.green}`,
            background: C.panel,
            display: "flex",
            justifyContent: "center",
            alignItems: "center",
          }}
        >
          <span
            style={{
              rotate: "-45deg",
              fontFamily: mono,
              fontSize: 34,
              color: C.green,
            }}
          >
            {"{ }"}
          </span>
        </div>
      </Enter>
      <Enter
        at={95}
        style={{
          position: "absolute",
          left: 1680,
          top: 766,
          fontFamily: mono,
          fontSize: 18,
          color: C.muted,
        }}
      >
        CANDIDATES
      </Enter>
      <Enter
        at={65}
        y={10}
        style={{
          position: "absolute",
          left: 100,
          top: 902,
          fontFamily: chinese,
          fontSize: 28,
        }}
      >
        代码按需读取 <span style={{ color: C.line, padding: "0 25px" }}>/</span>{" "}
        内置 Worthiness{" "}
        <span style={{ color: C.line, padding: "0 25px" }}>/</span> 全局并发限制{" "}
        <span style={{ fontSize: 20, color: C.muted, paddingLeft: 55 }}>
          维度由 Plan 动态生成 · 图为示意
        </span>
      </Enter>
    </Base>
  );
};
