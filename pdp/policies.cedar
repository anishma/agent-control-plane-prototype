permit(
  principal == Agent::"agent-client",
  action == Action::"tools/call",
  resource == Tool::"flight.book"
) when {
  context.delegator == "00000000-0000-0000-0000-000000000001" &&
  context.arguments.total_amount <= 2500
};

permit(
  principal == Agent::"agent-client",
  action == Action::"tools/call",
  resource == Tool::"credential.probe"
) when {
  context.delegator == "00000000-0000-0000-0000-000000000001"
};
