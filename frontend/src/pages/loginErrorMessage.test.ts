import assert from "node:assert/strict";
import test from "node:test";

import { getLoginErrorMessage } from "./loginErrorMessage.ts";

test("login error message treats unavailable backend as service startup", () => {
  assert.equal(
    getLoginErrorMessage({ response: { status: 503, data: { detail: "Service Unavailable" } } }),
    "系统正在启动，数据库连接可能尚未就绪，请稍后再试",
  );
});

test("login error message treats network errors as service startup", () => {
  assert.equal(
    getLoginErrorMessage({ code: "ERR_NETWORK" }),
    "系统正在启动，数据库连接可能尚未就绪，请稍后再试",
  );
});

test("login error message preserves invalid credential detail", () => {
  assert.equal(
    getLoginErrorMessage({ response: { status: 400, data: { detail: "邮箱或密码错误" } } }),
    "邮箱或密码错误",
  );
});
