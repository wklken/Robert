async page => {
  const origin = new URL(page.url()).origin;
  await page.goto(`${origin}/work`);
  const measurements = [];
  for (const width of [1440, 1200, 1050, 1000, 900, 700, 620, 375, 320]) {
    await page.setViewportSize({width, height: 900});
    const result = await page.locator('#filter-form').evaluate(form => {
      const bounds = form.getBoundingClientRect();
      const controls = [...form.querySelectorAll('input, select')].filter(e => e.getBoundingClientRect().width);
      return {
        overflow: controls.some(e => e.getBoundingClientRect().right > bounds.right + 1 || e.getBoundingClientRect().left < bounds.left - 1),
        rounded: controls.every(e => parseFloat(getComputedStyle(e).borderRadius) >= 6),
        pageOverflow: document.documentElement.scrollWidth > innerWidth,
      };
    });
    measurements.push({width, ...result});
    if (result.overflow || result.pageOverflow) throw new Error(`Filter overflow at ${width}px: ${JSON.stringify(result)}`);
    if (!result.rounded) throw new Error(`Filter controls must use individual rounded borders at ${width}px`);
  }
  await page.locator('#repo-filter').focus();
  const focus = await page.locator('#repo-filter').evaluate(e => ({
    outline: getComputedStyle(e).outlineStyle,
    offset: parseFloat(getComputedStyle(e).outlineOffset),
    width: parseFloat(getComputedStyle(e).outlineWidth),
  }));
  if (focus.outline === 'none' || focus.width < 1 || focus.offset > 0) throw new Error(`Expected a visible inset focus ring: ${JSON.stringify(focus)}`);
  return measurements;
}
