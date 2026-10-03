package com.zekam.fixture;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

class CalculatorTest {
    @Test
    void addsValues() {
        assertEquals(5, Calculator.add(2, 3));
    }

    @Test
    void classifiesZero() {
        assertEquals("zero", Calculator.classify(0));
    }
}
