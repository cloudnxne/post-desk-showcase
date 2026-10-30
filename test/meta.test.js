import { describe, it, expect } from "vitest";
import { metaStatus, titleStatus, META_MIN, META_MAX, TITLE_MAX } from "../src/core.js";

describe("meta description length", () => {
  it.each([
    [0, "short"],
    [META_MIN - 1, "short"],
    [META_MIN, "good"],
    [150, "good"],
    [META_MAX, "good"],
    [META_MAX + 1, "may truncate"],
  ])("%i characters is %s", (len, expected) => {
    expect(metaStatus(len)).toBe(expected);
  });

  it("the range Claude is asked for (140 to 158) always counts as good", () => {
    for (let len = 140; len <= 158; len++) expect(metaStatus(len)).toBe("good");
  });
});

describe("title tag length", () => {
  it("is good up to 60 characters and flagged after", () => {
    expect(titleStatus(TITLE_MAX)).toBe("good");
    expect(titleStatus(TITLE_MAX + 1)).toBe("may truncate");
  });
});
