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

export const Ownership = () => {
  const f = useCurrentFrame();
  return (
    <Base section="EXECUTION PLANE" number="02">
      <Enter at={5} style={{ position: "absolute", top: 176, left: 100 }}>
        <Eyebrow>DISPATCH IS NOT OWNERSHIP</Eyebrow>
        <Headline>
          ONE VALID <span style={{ color: C.green }}>OWNER.</span>
        </Headline>
      </Enter>
      <svg width="1920" height="1080" style={{ position: "absolute" }}>
        <Wire d="M420 592 C505 592 500 514 590 514" at={25} />
        <Wire d="M420 620 C510 620 510 720 590 720" at={30} color={C.muted} />
        <Wire d="M975 514 C1110 514 1090 526 1250 526" at={45} />
        <Wire
          d="M975 720 L1170 720 L1170 661 L1250 661"
          at={50}
          color={C.red}
          speed={130}
        />
        <path
          d="M840 450 C1040 333 1110 370 1015 465"
          fill="none"
          stroke={C.green}
          strokeWidth="2"
          strokeDasharray="5 9"
          opacity={progress(f, 60, 25)}
        />
      </svg>
      <Enter
        at={15}
        x={-50}
        y={0}
        style={{ position: "absolute", left: 100, top: 463 }}
      >
        <Panel style={{ width: 320, height: 295, padding: 28 }}>
          <div style={{ fontSize: 47, fontWeight: 600 }}>ARQ QUEUE</div>
          <div style={{ fontFamily: chinese, color: C.muted, fontSize: 25 }}>
            任务投递
          </div>
          {[0, 1, 2].map((i) => (
            <div
              key={i}
              style={{
                height: 36,
                marginTop: 13,
                border: `1px solid ${i === 0 ? C.green : C.line}`,
                display: "flex",
                alignItems: "center",
                paddingLeft: 13,
                gap: 14,
                translate: `${i === 0 ? Math.sin(f * 0.07) * 4 : 0}px 0`,
              }}
            >
              <div
                style={{
                  width: 7,
                  height: 7,
                  background: i === 0 ? C.green : C.muted,
                }}
              />
              <span
                style={{
                  fontFamily: mono,
                  fontSize: 16,
                  color: i === 0 ? C.green : C.muted,
                }}
              >
                TASK / {["READY", "QUEUED", "QUEUED"][i]}
              </span>
            </div>
          ))}
        </Panel>
      </Enter>
      <Enter
        at={25}
        y={35}
        style={{ position: "absolute", left: 590, top: 440 }}
      >
        <Panel
          style={{
            width: 385,
            height: 158,
            borderColor: C.green,
            padding: "22px 28px",
            boxShadow: `0 0 ${22 + 9 * Math.sin(f * 0.07)}px #d3f86a0d`,
          }}
        >
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
            }}
          >
            <span style={{ fontSize: 42, fontWeight: 600 }}>WORKER 01</span>
            <span
              style={{
                width: 12,
                height: 12,
                borderRadius: "50%",
                background: C.green,
              }}
            />
          </div>
          <div
            style={{
              fontFamily: mono,
              fontSize: 22,
              color: C.green,
              marginTop: 8,
            }}
          >
            LEASE / EPOCH 42
          </div>
          <div
            style={{
              fontFamily: chinese,
              fontSize: 22,
              color: C.muted,
              marginTop: 5,
            }}
          >
            租约认领 · 有效 owner
          </div>
        </Panel>
      </Enter>
      <Enter
        at={35}
        y={-20}
        style={{ position: "absolute", left: 590, top: 649 }}
      >
        <Panel style={{ width: 385, height: 149, padding: "22px 28px" }}>
          <div style={{ fontSize: 39, fontWeight: 600, color: C.muted }}>
            WORKER 02
          </div>
          <div
            style={{
              fontFamily: mono,
              fontSize: 21,
              color: C.red,
              marginTop: 8,
            }}
          >
            STALE / EPOCH 41
          </div>
          <div
            style={{
              fontFamily: chinese,
              fontSize: 21,
              color: C.muted,
              marginTop: 5,
            }}
          >
            旧执行者尝试提交
          </div>
        </Panel>
      </Enter>
      <Enter
        at={48}
        x={50}
        y={0}
        style={{ position: "absolute", left: 1250, top: 434 }}
      >
        <Panel style={{ width: 570, height: 346, padding: 30 }}>
          <div style={{ fontFamily: mono, color: C.muted, fontSize: 20 }}>
            CONTROL PLANE
          </div>
          <div style={{ fontSize: 57, fontWeight: 700, marginTop: 6 }}>
            COMMIT GATE
          </div>
          <div style={{ fontFamily: chinese, fontSize: 26, marginTop: 5 }}>
            身份 + 租约 + Fencing 校验
          </div>
          <div
            style={{
              borderTop: `1px solid ${C.line}`,
              marginTop: 25,
              paddingTop: 23,
              display: "flex",
              justifyContent: "space-between",
              fontFamily: mono,
              fontSize: 22,
            }}
          >
            <span style={{ color: C.green }}>42 → ACCEPT ✓</span>
            <span style={{ color: C.red }}>41 → BLOCK ×</span>
          </div>
          <div style={{ display: "flex", gap: 5, marginTop: 25 }}>
            {Array.from({ length: 20 }, (_, i) => (
              <div
                key={i}
                style={{
                  height: 9,
                  flex: 1,
                  background: i < 15 ? C.green : C.line,
                  opacity:
                    i < 15 ? 0.5 + 0.5 * Math.sin(f * 0.09 - i * 0.4) ** 2 : 1,
                }}
              />
            ))}
          </div>
        </Panel>
      </Enter>
      <Enter
        at={60}
        y={0}
        style={{
          position: "absolute",
          top: 378,
          left: 1090,
          fontFamily: mono,
          fontSize: 17,
          color: C.green,
        }}
      >
        HEARTBEAT ↻ RENEW
      </Enter>
      <Enter
        at={62}
        style={{
          position: "absolute",
          top: 868,
          left: 100,
          display: "flex",
          alignItems: "center",
          gap: 28,
        }}
      >
        <svg width="260" height="45">
          <path
            d="M0 23H70L85 23 96 7 108 40 121 13 132 23H260"
            fill="none"
            stroke={C.green}
            strokeWidth="3"
            strokeDasharray="360"
            strokeDashoffset={360 * (1 - progress(f % 90, 0, 55))}
          />
        </svg>
        <span style={{ fontFamily: chinese, fontSize: 32 }}>
          心跳续约 + 提交门禁，保证唯一有效 owner
        </span>
      </Enter>
    </Base>
  );
};
