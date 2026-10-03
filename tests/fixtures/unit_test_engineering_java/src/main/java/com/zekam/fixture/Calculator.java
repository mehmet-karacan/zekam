package com.zekam.fixture;

public final class Calculator {
    private Calculator() {}

    public static int add(int left, int right) {
        return left + right;
    }

    public static String classify(int value) {
        if (value < 0) {
            return "negative";
        }
        if (value == 0) {
            return "zero";
        }
        return "positive";
    }
}
