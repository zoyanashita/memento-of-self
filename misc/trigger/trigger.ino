const int buttonPin = 4;
int val = HIGH;
int lastVal = HIGH;

void setup() {
  Serial.begin(9600);
  pinMode(buttonPin, INPUT_PULLUP);
}

void loop() {
  val = digitalRead(buttonPin);
  if (val == LOW && lastVal == HIGH) {
    Serial.println("triggered");
  }
  lastVal = val;
  delay(50);
}