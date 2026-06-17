<script setup lang="ts">
import { DataZoomComponent, GridComponent, LegendComponent, TooltipComponent } from 'echarts/components'
import { init, use, type ECharts, type EChartsCoreOption } from 'echarts/core'
import { LineChart } from 'echarts/charts'
import { CanvasRenderer } from 'echarts/renderers'
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

use([CanvasRenderer, LineChart, GridComponent, LegendComponent, TooltipComponent, DataZoomComponent])

type TrendSeriesMeta = { key: string; name: string; color: string; yAxisIndex?: number }

const props = withDefaults(defineProps<{
  rows: Record<string, any>[]
  height?: number
  series?: TrendSeriesMeta[]
}>(), {
  height: 320,
})

const chartEl = ref<HTMLDivElement | null>(null)
let chart: ECharts | null = null
let resizeObserver: ResizeObserver | null = null

const defaultSeriesMeta: TrendSeriesMeta[] = [
  { key: 'sku_count', name: 'SKU', color: '#3b82f6' },
  { key: 'new_product_count', name: 'New Products', color: '#10b981' },
  { key: 'estimated_sales', name: 'Sales', color: '#f59e0b' },
  { key: 'estimated_revenue', name: 'Revenues', color: '#ef4444' },
  { key: 'traffic', name: 'Traffic', color: '#14b8a6' },
  { key: 'conversion_rate', name: 'Conversion Rate', color: '#6366f1' },
  { key: 'review_total', name: 'Reviews', color: '#8b5cf6' },
]

function isDarkTheme() {
  return document.documentElement.dataset.theme === 'dark'
}

function buildOption(): EChartsCoreOption {
  const seriesMeta = props.series?.length ? props.series : defaultSeriesMeta
  const dark = isDarkTheme()
  const axisColor = dark ? '#aab4c8' : '#9ca3af'
  const textColor = dark ? '#d5dcec' : '#6b7280'
  const gridColor = dark ? '#29213a' : '#e5e7eb'
  const tooltipBg = dark ? 'rgba(19,17,31,.96)' : 'rgba(255,255,255,.98)'
  const tooltipBorder = dark ? '#3d2d5a' : '#e5e7eb'
  const dates = props.rows.map((row) => String(row.date || ''))

  return {
    backgroundColor: 'transparent',
    color: seriesMeta.map((item) => item.color),
    animationDuration: 450,
    tooltip: {
      trigger: 'axis',
      confine: true,
      axisPointer: { type: 'line', lineStyle: { color: dark ? '#7c5fb5' : '#a78bfa', width: 1 } },
      backgroundColor: tooltipBg,
      borderColor: tooltipBorder,
      borderWidth: 1,
      textStyle: { color: dark ? '#edf0fb' : '#1f2329', fontSize: 12 },
      valueFormatter: (value: unknown) => Number(value || 0).toLocaleString(),
    },
    legend: {
      top: 0,
      type: 'scroll',
      icon: 'circle',
      itemWidth: 9,
      itemHeight: 9,
      textStyle: { color: textColor, fontSize: 12 },
    },
    grid: { top: 48, right: 48, bottom: 48, left: 58 },
    xAxis: {
      type: 'category',
      boundaryGap: false,
      data: dates,
      axisLine: { lineStyle: { color: gridColor } },
      axisTick: { show: false },
      axisLabel: { color: axisColor, formatter: (value: string) => value.slice(5) },
      splitLine: { show: false },
    },
    yAxis: [
      {
        type: 'value',
        axisLabel: { color: axisColor },
        splitLine: { lineStyle: { color: gridColor, type: 'dashed' } },
      },
      {
        type: 'value',
        min: 0,
        max: 5,
        axisLabel: { color: axisColor },
        splitLine: { show: false },
      },
    ],
    dataZoom: [
      { type: 'inside', throttle: 60 },
      {
        type: 'slider',
        height: 20,
        bottom: 8,
        borderColor: 'transparent',
        backgroundColor: dark ? '#0d0a17' : '#f3f4f6',
        fillerColor: dark ? 'rgba(167,139,250,.22)' : 'rgba(124,108,224,.18)',
        handleStyle: { color: dark ? '#a78bfa' : '#7c6ce0' },
        textStyle: { color: axisColor },
      },
    ],
    series: seriesMeta.map((meta) => ({
      name: meta.name,
      type: 'line',
      smooth: true,
      yAxisIndex: meta.yAxisIndex || 0,
      showSymbol: false,
      symbolSize: 6,
      emphasis: { focus: 'series' },
      lineStyle: { width: 2 },
      data: props.rows.map((row) => Number(row[meta.key] || 0)),
    })),
  }
}

async function renderChart() {
  await nextTick()
  if (!chartEl.value) return
  if (!chart) chart = init(chartEl.value)
  chart.setOption(buildOption(), true)
}

onMounted(() => {
  renderChart()
  if (chartEl.value) {
    resizeObserver = new ResizeObserver(() => chart?.resize())
    resizeObserver.observe(chartEl.value)
  }
})

onBeforeUnmount(() => {
  resizeObserver?.disconnect()
  chart?.dispose()
  chart = null
})

watch(() => [props.rows, props.series], renderChart, { deep: true })
</script>

<template>
  <div ref="chartEl" class="trend-line-chart" :style="{ height: `${height}px` }" />
</template>

<style scoped>
.trend-line-chart {
  width: 100%;
  min-height: 280px;
}
</style>
