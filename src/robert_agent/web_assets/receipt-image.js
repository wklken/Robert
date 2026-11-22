"use strict";

function paintReceiptText(context, node, origin, segmenter) {
  const style = getComputedStyle(node.parentElement);
  const range = document.createRange();
  let line = null;

  context.font = `${style.fontStyle} ${style.fontWeight} ${style.fontSize} ${style.fontFamily}`;
  context.fillStyle = style.color;
  context.direction = style.direction;
  const alignRight = style.textAlign === "right" || style.direction === "rtl";
  context.textAlign = alignRight ? "right" : "left";

  function paintLine() {
    if (!line) return;
    const metrics = context.measureText(line.text);
    const ascent = metrics.fontBoundingBoxAscent;
    const descent = metrics.fontBoundingBoxDescent;
    const baseline = line.top - origin.top + (line.height + ascent - descent) / 2;
    const x = (alignRight ? line.right : line.left) - origin.left;
    context.fillText(line.text, x, baseline);
    if (style.textDecorationLine.includes("underline")) {
      const offset = parseFloat(style.textUnderlineOffset) || 2;
      context.fillRect(line.left - origin.left, baseline + offset, line.right - line.left, 1);
    }
  }

  // The browser's layout determines wrapping, including CJK and long words.
  for (const { segment, index } of segmenter.segment(node.data)) {
    range.setStart(node, index);
    range.setEnd(node, index + segment.length);
    const box = range.getBoundingClientRect();
    if (!box.width || !box.height) continue;
    if (!line || line.top !== box.top) {
      paintLine();
      line = { top: box.top, height: box.height, left: box.left, right: box.right, text: "" };
    }
    line.left = Math.min(line.left, box.left);
    line.right = Math.max(line.right, box.right);
    line.text += /^\s+$/u.test(segment) ? " " : segment;
  }
  paintLine();
}

function paintReceiptPaper(context, paper, origin, toothDepth) {
  const style = getComputedStyle(paper);
  const width = origin.width;
  const height = origin.height;
  const radius = parseFloat(style.borderTopLeftRadius);
  context.fillStyle = style.backgroundColor;
  context.beginPath();
  context.roundRect(0, 0, width, height, [radius, radius, 0, 0]);
  context.fill();
  if (toothDepth) {
    context.save();
    context.beginPath();
    context.rect(0, height, width, toothDepth);
    context.clip();
    context.beginPath();
    context.moveTo(0, height);
    // Use the same pitch as the zigzag on the dialog.
    const pitch = toothDepth * 2;
    for (let x = -(pitch - width % pitch) / 2; x < width; x += pitch) {
      context.lineTo(x, height);
      context.lineTo(x + toothDepth, height + toothDepth);
      context.lineTo(x + pitch, height);
    }
    context.closePath();
    context.fill();
    context.restore();
  }

  context.setLineDash([3, 2]);
  for (const section of paper.querySelectorAll(".receipt-section")) {
    const style = getComputedStyle(section);
    const box = section.getBoundingClientRect();
    if (style.borderTopStyle !== "dashed" || !box.width) continue;
    const lineHeight = parseFloat(style.borderTopWidth);
    const y = box.top - origin.top + lineHeight / 2;
    context.strokeStyle = style.borderTopColor;
    context.lineWidth = lineHeight;
    context.beginPath();
    context.moveTo(box.left - origin.left, y);
    context.lineTo(box.right - origin.left, y);
    context.stroke();
  }
}

function renderReceiptCanvas(container) {
  const paper = container.querySelector(".receipt-paper");
  const header = paper.querySelector(".receipt-header");
  const logo = header.querySelector("img");
  const clone = paper.cloneNode(true);
  clone.setAttribute("aria-hidden", "true");
  clone.querySelectorAll("[data-receipt-control]").forEach((control) => control.remove());
  clone.querySelector(".receipt-header").style.minHeight = `${header.offsetHeight}px`;
  Object.assign(clone.style, {
    position: "fixed", left: "0", top: "0", width: getComputedStyle(paper).width,
    maxHeight: "none", overflow: "visible", visibility: "hidden",
  });

  const canvas = document.createElement("canvas");
  container.append(clone);
  try {
    const origin = clone.getBoundingClientRect();
    const toothDepth = parseFloat(getComputedStyle(container, "::after").height) || 0;
    const height = origin.height + toothDepth;
    const scale = Math.min(2, 16384 / height);
    canvas.width = Math.ceil(origin.width * scale);
    canvas.height = Math.ceil(height * scale);
    const context = canvas.getContext("2d");
    context.scale(scale, scale);
    paintReceiptPaper(context, clone, origin, toothDepth);

    const image = clone.querySelector(".receipt-brand img").getBoundingClientRect();
    context.drawImage(logo, image.left - origin.left, image.top - origin.top, image.width, image.height);

    const segmenter = new Intl.Segmenter(document.documentElement.lang, { granularity: "grapheme" });
    const walker = document.createTreeWalker(clone, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      if (walker.currentNode.data.trim()) paintReceiptText(context, walker.currentNode, origin, segmenter);
    }
  } finally {
    clone.remove();
  }
  return canvas;
}

async function renderReceiptImage(dialog) {
  const canvas = renderReceiptCanvas(dialog);
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => {
      if (blob) resolve(blob);
      else reject(new Error("Could not encode receipt image"));
    }, "image/png");
  });
}
