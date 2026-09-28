// What a submission would look like (starter: `pub fn two_sum(nums: Vec<i32>, target: i32) -> Vec<i32>`).
use std::collections::HashMap;
pub fn two_sum(nums: Vec<i32>, target: i32) -> Vec<i32> {
    let mut seen: HashMap<i32, i32> = HashMap::new();
    for (i, &n) in nums.iter().enumerate() {
        if let Some(&j) = seen.get(&(target - n)) { return vec![j, i as i32]; }
        seen.insert(n, i as i32);
    }
    vec![]
}
