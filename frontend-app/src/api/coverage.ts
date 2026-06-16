import { apiJson } from './client'

export function listCoverage() {
  return apiJson('/api/coverage')
}

export function dataQuality() {
  return apiJson('/api/data-quality')
}
