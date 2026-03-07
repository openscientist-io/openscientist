// Seed program — exists only to pull and compile all crate dependencies
// into the Docker image layer so agent code can use them without downloads.
use itertools::Itertools;
use ndarray::Array1;
use num_traits::Float;
use rand::Rng;
use rayon::prelude::*;
use serde::{Deserialize, Serialize};
use statrs::distribution::{ContinuousCDF, Normal};

#[derive(Serialize, Deserialize)]
struct _Dummy {
    x: f64,
}

fn main() {
    let _: Vec<i32> = (0..4).into_par_iter().map(|x| x * 2).collect();
    let _arr = Array1::<f64>::zeros(4);
    let n = Normal::new(0.0, 1.0).unwrap();
    let _ = n.cdf(1.96);
    let mut rng = rand::rng();
    let _: f64 = rng.random();
    let _ = serde_json::to_string(&_Dummy { x: 1.0 }).unwrap();
    let _ = (0..4).combinations(2).count();
    let _ = <f64 as Float>::sqrt(2.0);
    println!("seed ok");
}
