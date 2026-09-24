import { describe, expect, it } from 'vitest'
import { formatInput, formatSqlSeed, isOperationsInput } from './format'

describe('isOperationsInput', () => {
  it('matches the [ops, args] shape', () => {
    expect(isOperationsInput([['LRUCache', 'put'], [[2], [1, 1]]])).toBe(true)
  })

  it('rejects a flat positional-args array', () => {
    expect(isOperationsInput([[4, 9, 1, 6], 7])).toBe(false)
  })
})

describe('formatInput', () => {
  it('renders a function-kind case as a param-labeled arg list', () => {
    const params = [{ name: 'nums', type: 'List[int]' }, { name: 'target', type: 'int' }]
    expect(formatInput([[4, 9, 1, 6], 7], params, 'function')).toBe(
      'nums = [4,9,1,6], target = 7')
  })

  it('renders an operations-kind case as op(args); op(args)', () => {
    const input = [['LRUCache', 'put', 'get'], [[2], [1, 1], [1]]]
    expect(formatInput(input, [], 'operations')).toBe('LRUCache(2); put(1, 1); get(1)')
  })

  it('does not treat a function-kind case as operations even if it has the same shape', () => {
    // merge(intervals, newInterval): two array-typed params — same "two elements,
    // both arrays" shape as [ops, args], but kind="function" must not reformat it.
    const params = [{ name: 'intervals', type: 'List[List[int]]' },
                    { name: 'newInterval', type: 'List[int]' }]
    const input = [[[1, 3], [6, 9]], [2, 5]]
    expect(formatInput(input, params, 'function')).toBe(
      'intervals = [[1,3],[6,9]], newInterval = [2,5]')
  })

  it('renders a sql-kind case as its seed script, not a JSON-escaped array', () => {
    const input = ['CREATE TABLE t (x INT); INSERT INTO t VALUES (1);']
    expect(formatInput(input, [], 'sql')).toBe(
      'CREATE TABLE t (x INT);\nINSERT INTO t VALUES (1);')
  })
})

describe('formatSqlSeed', () => {
  it('puts one statement per line', () => {
    expect(formatSqlSeed("CREATE TABLE t (x INT); INSERT INTO t VALUES (1), (2);")).toBe(
      'CREATE TABLE t (x INT);\nINSERT INTO t VALUES (1), (2);')
  })

  it('does not leave a trailing blank line after the final statement', () => {
    expect(formatSqlSeed('SELECT 1; ')).toBe('SELECT 1;')
  })

  it('leaves a single statement with no trailing semicolon untouched', () => {
    expect(formatSqlSeed('SELECT 1')).toBe('SELECT 1')
  })
})
